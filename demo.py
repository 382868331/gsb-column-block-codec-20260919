"""演示：正常往返 + 范围读取 + 65 位 delta + 一次真实触发的 CRC 失败。"""

import struct
import sys
import zlib

import column_codec as cc

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def build_single_block_file(rows, enc_id, payload) -> bytes:
    n = len(rows)
    bitmap = bytearray((n + 7) // 8)
    for i, v in enumerate(rows):
        if v is None:
            bitmap[i >> 3] |= 1 << (i & 7)
    body = bytes([enc_id]) + bytes(bitmap) + payload
    block = body + struct.pack("<I", zlib.crc32(body))
    header = cc._HEADER.pack(cc.MAGIC, cc.VERSION, b"\x00\x00\x00", n, 1)
    dirent = cc._DIRENT.pack(cc._HEADER.size + cc._DIRENT.size, len(block), n)
    return header + dirent + block


def main() -> None:
    print("== 1. 正常编码 / 解码 / 范围读取 ==")
    values = (
        [7] * 100                                    # 块0: 常数（delta 位宽 0）
        + [None] * 28
        + [cc.INT64_MIN, cc.INT64_MAX] * 64          # 块1: int64 两端交替（raw）
        + [i * 3 - 50 for i in range(44)]            # 块2: 等差（delta 小位宽）
    )
    data = cc.encode(values)
    print(f"行数={len(values)} 编码后字节数={len(data)} 块数="
          f"{(len(values) + 127) // 128}")
    names = {cc.ENC_RAW: "raw", cc.ENC_DELTA: "delta", cc.ENC_RLE: "rle"}
    off = cc._HEADER.size + 3 * cc._DIRENT.size
    for i in range(3):
        blk_len = struct.unpack_from("<I", data, cc._HEADER.size + i * cc._DIRENT.size + 8)[0]
        print(f"  块{i}: 编码={names[data[off]]} 字节数={blk_len}")
        off += blk_len

    assert cc.decode(data) == values
    print("全量解码往返一致: True")

    # 跨 128 行块边界的范围读取，只解码相交块
    got, nblocks = cc.read_range(data, 120, 136)
    print(f"ReadRange(120,136) 命中 {nblocks} 块, 结果一致: {got == values[120:136]}")
    got, nblocks = cc.read_range(data, 200, 200)
    print(f"ReadRange(200,200) 空范围解码 {nblocks} 块, 结果: {got}")

    print("\n== 2. 65 位 delta（int64 两端相邻） ==")
    pair = [cc.INT64_MIN, cc.INT64_MAX]
    payload = cc._delta_payload(pair)
    width = payload[8]
    print(f"delta={cc.INT64_MAX - cc.INT64_MIN}, ZigZag 后位宽={width}")
    forced = build_single_block_file(pair, cc.ENC_DELTA, payload)
    print(f"强制 delta 块解码往返一致: {cc.decode(forced) == pair}")

    print("\n== 3. 真实触发的失败：CRC 校验 ==")
    bad = bytearray(data)
    bad[-10] ^= 0xFF  # 翻转最后一块载荷中的一个字节
    bad = bytes(bad)
    untouched, nblocks = cc.read_range(bad, 0, 10)  # 未读块校验延迟
    print(f"损坏末块后读取块0 仍成功: {untouched == values[:10]} (解码 {nblocks} 块)")
    try:
        cc.read_range(bad, 250, 270)
        print("错误：损坏数据未被检出")
    except cc.CodecError as exc:
        print(f"按预期触发失败: CodecError: {exc}")


if __name__ == "__main__":
    main()
