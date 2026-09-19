"""column_codec 演示：正常往返 + 随机范围核验 + 实际触发的失败。

运行：python demo.py（数秒内完成，所有结果由代码实际计算）。
"""

import random
import struct
import time

import column_codec as cc


def main():
    t0 = time.perf_counter()

    print("== 1. 构造混合列并编码 ==")
    rng = random.Random(20260920)
    vals = []
    for i in range(1000):
        r = rng.random()
        if r < 0.2:
            vals.append(None)
        elif r < 0.6:
            vals.append(1000 + i // 50)          # 缓慢变化，利于 delta/RLE
        elif r < 0.8:
            vals.append(rng.randrange(-10**9, 10**9))
        else:
            vals.append(rng.choice([cc.INT64_MIN, cc.INT64_MAX]))
    data = cc.encode(vals)
    n_blocks = (len(vals) + cc.BLOCK_ROWS - 1) // cc.BLOCK_ROWS
    print(f"行数={len(vals)}  块数={n_blocks}  编码后字节={len(data)}"
          f"  (原始 int64 需 {len(vals) * 8} 字节)")

    print("\n== 2. 全量解码往返核验 ==")
    back = cc.decode(data)
    assert back == vals
    print(f"往返一致：{back == vals}")

    print("\n== 3. 随机范围读取（固定种子，只解码相交块）==")
    for _ in range(5):
        a = rng.randrange(0, len(vals))
        b = rng.randrange(a, len(vals) + 1)
        res = cc.read_range(data, a, b)
        assert res.values == vals[a:b]
        print(f"ReadRange({a:4d},{b:4d}) -> {len(res.values):4d} 行,"
              f"解码 {res.blocks_decoded}/{n_blocks} 块, 与切片一致")

    print("\n== 4. 128 行块边界 ==")
    boundary = cc.encode(list(range(300)))  # 128 + 128 + 44
    res = cc.read_range(boundary, 127, 129)
    print(f"300 行 = 3 块; ReadRange(127,129) -> {res.values},"
          f"跨边界解码 {res.blocks_decoded} 块")
    res = cc.read_range(boundary, 0, 128)
    print(f"ReadRange(0,128) 恰好一块 -> 解码 {res.blocks_decoded} 块")

    print("\n== 5. 65 位 delta ==")
    d = cc.INT64_MAX - cc.INT64_MIN
    zz = cc._zigzag(d)
    print(f"delta(INT64_MIN->INT64_MAX) = {d} = 2**64-1")
    print(f"ZigZag(delta) = {zz}, 位宽 = {zz.bit_length()} 位")
    payload = cc._payload_delta([cc.INT64_MIN, cc.INT64_MAX,
                                 cc.INT64_MAX, cc.INT64_MIN])
    restored = cc._decode_delta(payload, 4)
    print(f"delta 载荷位宽字节 = {payload[8]}, 往返还原 = "
          f"{restored == [cc.INT64_MIN, cc.INT64_MAX, cc.INT64_MAX, cc.INT64_MIN]}")

    print("\n== 6. 实际触发的失败：篡改载荷一字节 ==")
    corrupted = bytearray(data)
    corrupted[-5] ^= 0xFF  # 末块载荷最后一字节，CRC 不变
    try:
        cc.decode(bytes(corrupted))
        print("意外：损坏数据被接受")
    except cc.CodecError as exc:
        print(f"按预期拒绝损坏数据：CodecError: {exc}")

    print("\n== 7. 延迟校验：未读块损坏不影响范围读取 ==")
    res = cc.read_range(bytes(corrupted), 0, 100)
    print(f"ReadRange(0,100) 在损坏文件中仍成功，解码 {res.blocks_decoded} 块,"
          f"首行 = {res.values[0]!r}")

    print(f"\n演示完成，用时 {time.perf_counter() - t0:.2f} 秒")


if __name__ == "__main__":
    main()
