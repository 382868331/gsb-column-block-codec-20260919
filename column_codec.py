"""可随机定位的列式可空整数块编码库。

格式概览（全部多字节字段为小端；精确布局见 README.md）：

    文件 = 文件头 + 块目录 + 块[0] + 块[1] + ... （块连续无空洞）
    文件头 = magic(4) + version(u8) + reserved(3, 全零) + total_rows(u64) + block_count(u32)
    目录项 = offset(u64) + length(u32) + row_count(u32)
    块 = encoding(u8) + null_bitmap(ceil(row_count/8)) + payload + crc32(u32)

crc32 覆盖 encoding、null_bitmap 与 payload（不含自身）。
"""

from __future__ import annotations

import struct
import zlib

MAGIC = b"CIB1"
VERSION = 1
BLOCK_ROWS = 128
INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

# 构造（加载）时的默认上限：声明行数与输入字节数。
DEFAULT_MAX_ROWS = 1 << 26
DEFAULT_MAX_BYTES = 1 << 31

ENC_RAW = 0
ENC_DELTA = 1
ENC_RLE = 2

_HEADER = struct.Struct("<4sB3sQI")  # 20 字节；reserved 必须为全零
_DIRENT = struct.Struct("<QII")       # 16 字节
_U32 = struct.Struct("<I")
_I64 = struct.Struct("<q")
_RLE_RUN = struct.Struct("<qI")       # value + count


class CodecError(ValueError):
    """格式、校验或边界违规。"""


# ---------------------------------------------------------------- 位打包

def _zigzag(d: int) -> int:
    return 2 * d if d >= 0 else -2 * d - 1


def _unzigzag(z: int) -> int:
    return z // 2 if z % 2 == 0 else -((z + 1) // 2)


def _pack_bits(values, width: int) -> bytes:
    """把无符号整数按统一位宽打包，高位在前，末尾未用位补 0。"""
    if width == 0:
        return b""
    out = bytearray()
    acc = 0
    nbits = 0
    for v in values:
        acc = (acc << width) | v
        nbits += width
        while nbits >= 8:
            nbits -= 8
            out.append((acc >> nbits) & 0xFF)
            acc &= (1 << nbits) - 1
    if nbits:
        out.append((acc << (8 - nbits)) & 0xFF)
    return bytes(out)


def _unpack_bits(data: bytes, count: int, width: int) -> list[int]:
    if width == 0:
        if data:
            raise CodecError("位宽为 0 但存在打包字节")
        return [0] * count
    expected = (count * width + 7) // 8
    if len(data) != expected:
        raise CodecError("打包位流长度不符（截断或多余）")
    total = int.from_bytes(data, "big")
    pad = len(data) * 8 - count * width
    if pad and (total & ((1 << pad) - 1)):
        raise CodecError("打包位流尾部 padding 非零")
    mask = (1 << width) - 1
    return [(total >> (pad + (count - 1 - i) * width)) & mask for i in range(count)]


# ---------------------------------------------------------------- 载荷编码

def _raw_payload(nonnull: list[int]) -> bytes:
    return b"".join(_I64.pack(v) for v in nonnull)


def _delta_payload(nonnull: list[int]) -> bytes:
    if not nonnull:
        return b""
    out = bytearray(_I64.pack(nonnull[0]))
    if len(nonnull) < 2:
        return bytes(out)
    zz = [_zigzag(b - a) for a, b in zip(nonnull, nonnull[1:])]
    width = max(z.bit_length() for z in zz)
    out.append(width)
    out += _pack_bits(zz, width)
    return bytes(out)


def _rle_payload(nonnull: list[int]) -> bytes:
    out = bytearray()
    i = 0
    n = len(nonnull)
    while i < n:
        j = i + 1
        while j < n and nonnull[j] == nonnull[i]:
            j += 1
        out += _RLE_RUN.pack(nonnull[i], j - i)
        i = j
    return bytes(out)


# ---------------------------------------------------------------- 块编码

def _encode_block(rows: list) -> bytes:
    n = len(rows)
    bitmap = bytearray((n + 7) // 8)
    nonnull = []
    for i, v in enumerate(rows):
        if v is None:
            bitmap[i >> 3] |= 1 << (i & 7)
        else:
            nonnull.append(v)
    bitmap = bytes(bitmap)
    best = None
    # 平局保持顺序：raw < delta < rle。
    for enc_id, payload in (
        (ENC_RAW, _raw_payload(nonnull)),
        (ENC_DELTA, _delta_payload(nonnull)),
        (ENC_RLE, _rle_payload(nonnull)),
    ):
        body = bytes([enc_id]) + bitmap + payload
        block = body + _U32.pack(zlib.crc32(body))
        if best is None or len(block) < len(best):
            best = block
    return best


def encode(values, *, max_rows: int = DEFAULT_MAX_ROWS) -> bytes:
    """把 int64/None 序列编码为列块文件字节串。"""
    vals = list(values)
    if len(vals) > max_rows:
        raise CodecError(f"行数 {len(vals)} 超过上限 {max_rows}")
    for v in vals:
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            raise CodecError(f"非法值类型: {type(v).__name__}")
        if not (INT64_MIN <= v <= INT64_MAX):
            raise CodecError(f"值超出 int64 范围: {v}")
    blocks = [_encode_block(vals[i:i + BLOCK_ROWS])
              for i in range(0, len(vals), BLOCK_ROWS)]
    header = _HEADER.pack(MAGIC, VERSION, b"\x00\x00\x00", len(vals), len(blocks))
    offset = _HEADER.size + _DIRENT.size * len(blocks)
    directory = bytearray()
    for blk in blocks:
        directory += _DIRENT.pack(offset, len(blk), 0)  # row_count 稍后填
        offset += len(blk)
    # 填 row_count
    for i in range(len(blocks)):
        rows = min(BLOCK_ROWS, len(vals) - i * BLOCK_ROWS)
        struct.pack_into("<I", directory, i * _DIRENT.size + 12, rows)
    return header + bytes(directory) + b"".join(blocks)


# ---------------------------------------------------------------- 加载校验

class _File:
    __slots__ = ("data", "total_rows", "entries")


def _load(data, max_rows: int, max_bytes: int) -> _File:
    if not isinstance(data, (bytes, bytearray, memoryview)):
        raise CodecError("输入必须是字节串")
    data = bytes(data)
    if len(data) > max_bytes:
        raise CodecError(f"输入字节数 {len(data)} 超过上限 {max_bytes}")
    if len(data) < _HEADER.size:
        raise CodecError("输入截断：不足文件头")
    magic, version, reserved, total_rows, block_count = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise CodecError("magic 不匹配")
    if version != VERSION:
        raise CodecError(f"不支持的版本: {version}")
    if reserved != b"\x00\x00\x00":
        raise CodecError("文件头保留字节 padding 非零")
    if total_rows > max_rows:
        raise CodecError(f"声明行数 {total_rows} 超过上限 {max_rows}")
    expected_blocks = (total_rows + BLOCK_ROWS - 1) // BLOCK_ROWS
    if block_count != expected_blocks:
        raise CodecError("块数量与总行数不符")
    dir_end = _HEADER.size + block_count * _DIRENT.size
    if len(data) < dir_end:
        raise CodecError("输入截断：不足块目录")
    entries = []
    prev_end = dir_end
    rows_sum = 0
    for i in range(block_count):
        off, length, rows = _DIRENT.unpack_from(data, _HEADER.size + i * _DIRENT.size)
        if off != prev_end:
            raise CodecError("块目录不连续（存在空洞或重叠）")
        if rows < 1 or rows > BLOCK_ROWS:
            raise CodecError("块行数越界")
        if i < block_count - 1 and rows != BLOCK_ROWS:
            raise CodecError("非末块行数不足 128")
        min_len = 1 + (rows + 7) // 8 + _U32.size
        if length < min_len:
            raise CodecError("块长度小于最小可能值")
        if off + length > len(data):
            raise CodecError("块超出输入末尾（截断）")
        prev_end = off + length
        rows_sum += rows
        entries.append((off, length, rows))
    if prev_end != len(data):
        raise CodecError("块目录之后存在多余字节")
    if rows_sum != total_rows:
        raise CodecError("目录行数之和与声明总行数不符")
    f = _File()
    f.data = data
    f.total_rows = total_rows
    f.entries = entries
    return f


# ---------------------------------------------------------------- 块解码

def _decode_block(buf: bytes, rows: int) -> list:
    bm_len = (rows + 7) // 8
    if len(buf) < 1 + bm_len + _U32.size:
        raise CodecError("块截断")
    body, crc_stored = buf[:-_U32.size], _U32.unpack_from(buf, len(buf) - _U32.size)[0]
    if zlib.crc32(body) != crc_stored:
        raise CodecError("块 CRC32 校验失败")
    enc_id = body[0]
    if enc_id not in (ENC_RAW, ENC_DELTA, ENC_RLE):
        raise CodecError(f"未知编码标识: {enc_id}")
    bitmap = body[1:1 + bm_len]
    tail = rows % 8
    if tail and (bitmap[-1] & ~((1 << tail) - 1)):
        raise CodecError("空值位图尾部 padding 非零")
    null_flags = [(bitmap[i >> 3] >> (i & 7)) & 1 for i in range(rows)]
    nn = rows - sum(null_flags)
    payload = body[1 + bm_len:]

    if enc_id == ENC_RAW:
        if len(payload) != 8 * nn:
            raise CodecError("raw 载荷长度不符")
        nonnull = [_I64.unpack_from(payload, 8 * i)[0] for i in range(nn)]
    elif enc_id == ENC_DELTA:
        if nn == 0:
            if payload:
                raise CodecError("全空块存在 delta 载荷")
            nonnull = []
        else:
            if len(payload) < 8:
                raise CodecError("delta 载荷截断：缺少首值")
            first = _I64.unpack_from(payload, 0)[0]
            if nn == 1:
                if len(payload) != 8:
                    raise CodecError("单非空值块存在多余 delta 字节")
                nonnull = [first]
            else:
                width = payload[8]
                if width > 65:
                    raise CodecError(f"非法 delta 位宽: {width}")
                zz = _unpack_bits(payload[9:], nn - 1, width)
                nonnull = [first]
                cur = first
                for z in zz:
                    cur += _unzigzag(z)
                    if not (INT64_MIN <= cur <= INT64_MAX):
                        raise CodecError("delta 还原值超出 int64 范围")
                    nonnull.append(cur)
    else:  # ENC_RLE
        if len(payload) % _RLE_RUN.size:
            raise CodecError("RLE 载荷长度不符")
        nonnull = []
        for off in range(0, len(payload), _RLE_RUN.size):
            value, count = _RLE_RUN.unpack_from(payload, off)
            if count < 1:
                raise CodecError("RLE 游程长度为零")
            nonnull.extend([value] * count)
        if len(nonnull) != nn:
            raise CodecError("RLE 游程长度之和与非空值数量不符")

    out = []
    it = iter(nonnull)
    for is_null in null_flags:
        out.append(None if is_null else next(it))
    return out


# ---------------------------------------------------------------- 公开读取接口

def read_range(data, start: int, stop: int, *,
               max_rows: int = DEFAULT_MAX_ROWS,
               max_bytes: int = DEFAULT_MAX_BYTES):
    """解码 [start, stop) 范围的行，返回 (values, 实际解码块数)。

    仅解码与范围相交的块；空范围解码 0 块。
    """
    f = _load(data, max_rows, max_bytes)
    if not (0 <= start <= stop <= f.total_rows):
        raise CodecError(
            f"范围越界: 需要 0 <= start <= stop <= {f.total_rows}, 得到 [{start}, {stop})")
    if start == stop:
        return [], 0
    out = []
    decoded = 0
    row_base = 0
    for off, length, rows in f.entries:
        block_start, block_stop = row_base, row_base + rows
        row_base = block_stop
        if block_stop <= start or block_start >= stop:
            continue
        block_rows = _decode_block(f.data[off:off + length], rows)
        decoded += 1
        lo = max(start, block_start) - block_start
        hi = min(stop, block_stop) - block_start
        out.extend(block_rows[lo:hi])
    return out, decoded


def decode(data, *, max_rows: int = DEFAULT_MAX_ROWS,
           max_bytes: int = DEFAULT_MAX_BYTES) -> list:
    """全量解码，返回长度恰为声明总行数的列表。"""
    f = _load(data, max_rows, max_bytes)
    out = []
    for off, length, rows in f.entries:
        out.extend(_decode_block(f.data[off:off + length], rows))
    return out
