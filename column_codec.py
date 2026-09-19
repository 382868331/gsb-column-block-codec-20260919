"""可随机定位的列式可空 int64 块编解码库。

格式（全部多字节字段为小端序）::

    文件头（17 字节）
        magic       4s   b"CIB1"
        version     B    1
        total_rows  Q    总行数
        block_count I    块数 = ceil(total_rows / 128)
    块目录（block_count 项，每项 14 字节，连续无空洞）
        offset      Q    块记录相对文件起始的偏移
        length      I    块记录字节数（含 CRC）
        row_count   H    本块行数（仅末块可 < 128）
    块记录（紧跟目录之后，首尾相接）
        encoding    B    0=raw 1=delta 2=rle
        null_bitmap ceil(row_count/8) 字节，bit i 置位表示第 i 行为 NULL，
                    LSB 优先，未使用尾位必须为 0
        payload     依编码而定（见下）
        crc32       I    覆盖 encoding + null_bitmap + payload

    payload:
        raw    nnz 个 int64（非空值按行序）
        delta  首个非空值 int64；非空值 >= 2 时追加位宽 B（0..65）及
               (nnz-1) 个 ZigZag delta 的统一位宽打包（LSB 优先，尾位为 0）；
               非空值不足 2 时无 delta 项，全空块不存首值
        rle    若干 (value int64, run_length uint32) 对，run_length >= 1，
               覆盖全部非空值序列

仅依赖标准库，Windows 原生 Python 3.14 可离线运行。
"""

from __future__ import annotations

import struct
import zlib
from typing import NamedTuple

MAGIC = b"CIB1"
VERSION = 1
BLOCK_ROWS = 128

# 构造期限定：单次加载允许的最大行数与最大输入字节数
MAX_ROWS = 1 << 26
MAX_INPUT_BYTES = 1 << 26

ENC_RAW = 0
ENC_DELTA = 1
ENC_RLE = 2

INT64_MIN = -(1 << 63)
INT64_MAX = (1 << 63) - 1

MAX_BIT_WIDTH = 65  # ZigZag(±(2**64 - 1)) 最高需要 65 位

_HEADER = struct.Struct("<4sBQI")   # magic, version, total_rows, block_count
_DIRENT = struct.Struct("<QIH")     # offset, length, row_count
HEADER_SIZE = _HEADER.size
DIRENT_SIZE = _DIRENT.size
_RLE_RUN = struct.Struct("<qI")     # value, run_length


class CodecError(ValueError):
    """输入非法或编码数据损坏时抛出。"""


class RangeResult(NamedTuple):
    """ReadRange 的结果：命中的行与实际解码的块数。"""

    values: list
    blocks_decoded: int


# ---------------------------------------------------------------- 位打包

def _pack_bits(values, width):
    """把无符号整数序列按统一位宽 LSB 优先打包成字节串。"""
    nbits = len(values) * width
    nbytes = (nbits + 7) // 8
    acc = 0
    for i, v in enumerate(values):
        acc |= v << (i * width)
    return acc.to_bytes(nbytes, "little")


def _unpack_bits(data, count, width):
    """解包统一位宽整数序列，校验长度精确且未使用尾位为 0。"""
    nbits = count * width
    if len(data) != (nbits + 7) // 8:
        raise CodecError("packed delta payload length mismatch")
    acc = int.from_bytes(data, "little")
    if nbits % 8 and acc >> nbits:
        raise CodecError("non-zero padding bits in packed deltas")
    if width == 0:
        return [0] * count
    mask = (1 << width) - 1
    return [(acc >> (i * width)) & mask for i in range(count)]


def _zigzag(d):
    return 2 * d if d >= 0 else -2 * d - 1


def _unzigzag(z):
    return z // 2 if z % 2 == 0 else -((z + 1) // 2)


# ---------------------------------------------------------------- 载荷编码

def _payload_raw(vals):
    return struct.pack("<%dq" % len(vals), *vals) if vals else b""


def _payload_delta(vals):
    if not vals:
        return b""
    out = bytearray(struct.pack("<q", vals[0]))
    if len(vals) >= 2:
        zz = [_zigzag(vals[i + 1] - vals[i]) for i in range(len(vals) - 1)]
        width = max(z.bit_length() for z in zz)
        out.append(width)
        out += _pack_bits(zz, width)
    return bytes(out)


def _payload_rle(vals):
    out = bytearray()
    i = 0
    n = len(vals)
    while i < n:
        j = i
        while j + 1 < n and vals[j + 1] == vals[i]:
            j += 1
        out += _RLE_RUN.pack(vals[i], j - i + 1)
        i = j + 1
    return bytes(out)


def _decode_delta(payload, nnz):
    if nnz == 0:
        if payload:
            raise CodecError("delta payload must be empty for all-null block")
        return []
    if len(payload) < 8:
        raise CodecError("truncated delta first value")
    first = struct.unpack_from("<q", payload, 0)[0]
    if nnz == 1:
        if len(payload) != 8:
            raise CodecError("delta payload length mismatch for single value")
        return [first]
    if len(payload) < 9:
        raise CodecError("truncated delta bit width")
    width = payload[8]
    if width > MAX_BIT_WIDTH:
        raise CodecError("invalid delta bit width")
    zz = _unpack_bits(payload[9:], nnz - 1, width)
    vals = [first]
    cur = first
    for z in zz:
        cur += _unzigzag(z)
        if not (INT64_MIN <= cur <= INT64_MAX):
            raise CodecError("delta reconstruction overflows int64")
        vals.append(cur)
    return vals


# ---------------------------------------------------------------- 块编解码

def _null_bitmap(rows):
    n = len(rows)
    out = bytearray((n + 7) // 8)
    for i, v in enumerate(rows):
        if v is None:
            out[i // 8] |= 1 << (i % 8)
    return bytes(out)


def _encode_block(rows):
    """编码一个不超过 128 行的块，按完整块字节数选最短，平局按 raw/delta/rle。"""
    vals = [v for v in rows if v is not None]
    bitmap = _null_bitmap(rows)
    best = None
    for enc, payload in (
        (ENC_RAW, _payload_raw(vals)),
        (ENC_DELTA, _payload_delta(vals)),
        (ENC_RLE, _payload_rle(vals)),
    ):
        body = bytes([enc]) + bitmap + payload
        block = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
        if best is None or len(block) < len(best):
            best = block
    return best


def _decode_block(data, offset, length, row_count):
    raw = data[offset:offset + length]
    body, crc_stored = raw[:-4], struct.unpack_from("<I", raw, length - 4)[0]
    if zlib.crc32(body) & 0xFFFFFFFF != crc_stored:
        raise CodecError("block CRC32 mismatch")
    enc = body[0]
    nb = (row_count + 7) // 8
    if len(body) < 1 + nb:
        raise CodecError("truncated null bitmap")
    bitmap = body[1:1 + nb]
    payload = body[1 + nb:]
    if row_count % 8 and bitmap[-1] >> (row_count % 8):
        raise CodecError("non-zero padding bits in null bitmap")
    nulls = [(bitmap[i // 8] >> (i % 8)) & 1 for i in range(row_count)]
    nnz = row_count - sum(nulls)

    if enc == ENC_RAW:
        if len(payload) != nnz * 8:
            raise CodecError("raw payload length mismatch")
        vals = list(struct.unpack("<%dq" % nnz, payload)) if nnz else []
    elif enc == ENC_DELTA:
        vals = _decode_delta(payload, nnz)
    elif enc == ENC_RLE:
        if len(payload) % _RLE_RUN.size:
            raise CodecError("rle payload length mismatch")
        vals = []
        for j in range(0, len(payload), _RLE_RUN.size):
            v, run = _RLE_RUN.unpack_from(payload, j)
            if run < 1:
                raise CodecError("rle run length must be >= 1")
            vals.extend([v] * run)
        if len(vals) != nnz:
            raise CodecError("rle run total does not match non-null count")
    else:
        raise CodecError("unknown block encoding")

    out = []
    it = iter(vals)
    for is_null in nulls:
        out.append(None if is_null else next(it))
    return out


# ---------------------------------------------------------------- 文件装载

class _Layout(NamedTuple):
    data: bytes
    total_rows: int
    blocks: list  # (offset, length, row_count)


def _load(data, max_rows, max_bytes):
    if isinstance(data, (bytearray, memoryview)):
        data = bytes(data)
    if not isinstance(data, bytes):
        raise CodecError("encoded input must be bytes-like")
    if len(data) > max_bytes:
        raise CodecError("input exceeds byte limit")
    if len(data) < HEADER_SIZE:
        raise CodecError("truncated header")
    magic, version, total_rows, block_count = _HEADER.unpack_from(data, 0)
    if magic != MAGIC:
        raise CodecError("bad magic")
    if version != VERSION:
        raise CodecError("unsupported version")
    if total_rows > max_rows:
        raise CodecError("declared row count exceeds limit")
    if block_count != (total_rows + BLOCK_ROWS - 1) // BLOCK_ROWS:
        raise CodecError("block count inconsistent with total rows")
    dir_end = HEADER_SIZE + DIRENT_SIZE * block_count
    if len(data) < dir_end:
        raise CodecError("truncated block directory")

    blocks = []
    offset = dir_end
    for i in range(block_count):
        off, length, rc = _DIRENT.unpack_from(data, HEADER_SIZE + i * DIRENT_SIZE)
        if off != offset:
            raise CodecError("block directory has a gap or overlap")
        if length < 1 + 4:
            raise CodecError("block record too short")
        if off + length > len(data):
            raise CodecError("block record out of bounds (truncated data)")
        expected_rc = (
            BLOCK_ROWS if i < block_count - 1
            else total_rows - BLOCK_ROWS * (block_count - 1)
        )
        if rc != expected_rc:
            raise CodecError("block row count inconsistent with total rows")
        blocks.append((off, length, rc))
        offset += length
    if offset != len(data):
        raise CodecError("trailing bytes after last block")
    return _Layout(data, total_rows, blocks)


# ---------------------------------------------------------------- 公开接口

def encode(values, *, max_rows=MAX_ROWS):
    """把 int64/None 序列编码为列块字节串。"""
    vals = list(values)
    if len(vals) > max_rows:
        raise CodecError("row count exceeds limit")
    for v in vals:
        if v is None:
            continue
        if isinstance(v, bool) or not isinstance(v, int):
            raise CodecError("values must be int or None")
        if not (INT64_MIN <= v <= INT64_MAX):
            raise CodecError("value out of int64 range")
    blocks = [vals[i:i + BLOCK_ROWS] for i in range(0, len(vals), BLOCK_ROWS)]
    encoded = [_encode_block(b) for b in blocks]
    header = _HEADER.pack(MAGIC, VERSION, len(vals), len(blocks))
    offset = HEADER_SIZE + DIRENT_SIZE * len(blocks)
    directory = bytearray()
    for blk, enc in zip(blocks, encoded):
        directory += _DIRENT.pack(offset, len(enc), len(blk))
        offset += len(enc)
    return header + bytes(directory) + b"".join(encoded)


def decode(data, *, max_rows=MAX_ROWS, max_bytes=MAX_INPUT_BYTES):
    """全量解码，返回与编码输入等长的 int/None 列表。"""
    layout = _load(data, max_rows, max_bytes)
    out = []
    for off, length, rc in layout.blocks:
        out.extend(_decode_block(layout.data, off, length, rc))
    return out


def read_range(data, start, stop, *, max_rows=MAX_ROWS,
               max_bytes=MAX_INPUT_BYTES):
    """只解码与 [start, stop) 相交的块，返回 RangeResult。"""
    if isinstance(start, bool) or isinstance(stop, bool) \
            or not isinstance(start, int) or not isinstance(stop, int):
        raise CodecError("range bounds must be integers")
    layout = _load(data, max_rows, max_bytes)
    if not (0 <= start <= stop <= layout.total_rows):
        raise CodecError("range out of bounds")
    if start == stop:
        return RangeResult([], 0)
    values = []
    blocks_decoded = 0
    first_blk = start // BLOCK_ROWS
    last_blk = (stop - 1) // BLOCK_ROWS
    for i in range(first_blk, last_blk + 1):
        off, length, rc = layout.blocks[i]
        rows = _decode_block(layout.data, off, length, rc)
        blocks_decoded += 1
        base = i * BLOCK_ROWS
        lo = max(start, base) - base
        hi = min(stop, base + rc) - base
        values.extend(rows[lo:hi])
    return RangeResult(values, blocks_decoded)
