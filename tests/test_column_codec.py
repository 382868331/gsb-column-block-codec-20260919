"""column_codec 的单元测试。"""

import random
import struct
import unittest
import zlib

import column_codec as cc


def rebuild_with_block(data, block_index, new_block):
    """替换文件中的第 block_index 个块并保持目录连续（仅测试用）。"""
    magic, version, total_rows, block_count = struct.unpack_from("<4sBQI", data, 0)
    entries = [struct.unpack_from("<QIH", data, cc.HEADER_SIZE + i * cc.DIRENT_SIZE)
               for i in range(block_count)]
    off, length, rc = entries[block_index]
    assert len(new_block) == length, "测试辅助函数要求等长替换"
    return data[:off] + new_block + data[off + length:]


def mutate_block(data, block_index, fn):
    """对第 block_index 个块的 body（不含 CRC）应用 fn 并重算 CRC。"""
    magic, version, total_rows, block_count = struct.unpack_from("<4sBQI", data, 0)
    off, length, rc = struct.unpack_from(
        "<QIH", data, cc.HEADER_SIZE + block_index * cc.DIRENT_SIZE)
    body = bytearray(data[off:off + length - 4])
    fn(body)
    new_block = bytes(body) + struct.pack("<I", zlib.crc32(bytes(body)) & 0xFFFFFFFF)
    return rebuild_with_block(data, block_index, new_block)


class RoundTripTests(unittest.TestCase):
    def test_int64_extremes_alternating(self):
        vals = [cc.INT64_MIN, cc.INT64_MAX] * 64  # 128 行，两端交替
        data = cc.encode(vals)
        self.assertEqual(cc.decode(data), vals)

    def test_none_interleaved_rle(self):
        # 非空值序列为 [7]*32 + [9]*32（长游程），None 间隔出现：
        # rle=24 字节 < delta=33 字节 < raw=512 字节，RLE 应被选中
        vals = []
        for i in range(200):
            if i % 2:
                vals.append(None)
            else:
                vals.append(7 if (i // 2) % 64 < 32 else 9)
        data = cc.encode(vals)
        self.assertEqual(cc.decode(data), vals)
        # 首块 encoding 字节应为 RLE
        off = struct.unpack_from("<QIH", data, cc.HEADER_SIZE)[0]
        self.assertEqual(data[off], cc.ENC_RLE)

    def test_block_boundary_128(self):
        vals = list(range(300))  # 3 块：128 + 128 + 44
        data = cc.encode(vals)
        self.assertEqual(cc.decode(data), vals)
        # 跨块范围解码 2 个块
        res = cc.read_range(data, 127, 129)
        self.assertEqual(res.values, [127, 128])
        self.assertEqual(res.blocks_decoded, 2)
        # 单块内范围只解码 1 个块
        res = cc.read_range(data, 0, 128)
        self.assertEqual(res.values, list(range(128)))
        self.assertEqual(res.blocks_decoded, 1)
        # 末块不足 128 行
        res = cc.read_range(data, 256, 300)
        self.assertEqual(res.values, list(range(256, 300)))
        self.assertEqual(res.blocks_decoded, 1)

    def test_all_null_block(self):
        vals = [None] * 130
        data = cc.encode(vals)
        self.assertEqual(cc.decode(data), vals)

    def test_constant_column(self):
        vals = [-42] * 257
        data = cc.encode(vals)
        self.assertEqual(cc.decode(data), vals)

    def test_single_row(self):
        for vals in ([5], [None], [cc.INT64_MIN], [cc.INT64_MAX]):
            self.assertEqual(cc.decode(cc.encode(vals)), vals)

    def test_empty_column(self):
        data = cc.encode([])
        self.assertEqual(cc.decode(data), [])
        res = cc.read_range(data, 0, 0)
        self.assertEqual(res.values, [])
        self.assertEqual(res.blocks_decoded, 0)

    def test_delta_65_bit(self):
        # INT64_MIN -> INT64_MAX 的 delta 为 2**64-1，ZigZag 后需 65 位
        first, second = cc.INT64_MIN, cc.INT64_MAX
        payload = cc._payload_delta([first, second, second, first])
        self.assertEqual(payload[8], 65)  # 位宽字节
        self.assertEqual(cc._decode_delta(payload, 4),
                         [first, second, second, first])
        # 手工构造一个 delta 编码的块，走完整文件解码路径
        vals = [first, second] * 64
        bitmap = bytes(16)  # 128 行无空值
        body = bytes([cc.ENC_DELTA]) + bitmap + cc._payload_delta(vals)
        block = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
        header = struct.pack("<4sBQI", cc.MAGIC, cc.VERSION, 128, 1)
        dirent = struct.pack("<QIH", cc.HEADER_SIZE + cc.DIRENT_SIZE,
                             len(block), 128)
        self.assertEqual(cc.decode(header + dirent + block), vals)

    def test_random_roundtrip_and_ranges(self):
        rng = random.Random(20260920)
        for _ in range(20):
            n = rng.randrange(0, 400)
            vals = []
            for _ in range(n):
                r = rng.random()
                if r < 0.25:
                    vals.append(None)
                elif r < 0.5:
                    vals.append(rng.choice([0, 1, -1, 7]))
                elif r < 0.75:
                    vals.append(rng.randrange(-1000, 1000))
                else:
                    vals.append(rng.choice([cc.INT64_MIN, cc.INT64_MAX,
                                            rng.randrange(cc.INT64_MIN,
                                                          cc.INT64_MAX)]))
            data = cc.encode(vals)
            self.assertEqual(cc.decode(data), vals)
            for _ in range(10):
                a = rng.randrange(0, n + 1)
                b = rng.randrange(a, n + 1)
                res = cc.read_range(data, a, b)
                self.assertEqual(res.values, vals[a:b])
                expected_blocks = (
                    0 if a == b
                    else (b - 1) // cc.BLOCK_ROWS - a // cc.BLOCK_ROWS + 1)
                self.assertEqual(res.blocks_decoded, expected_blocks)


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        rng = random.Random(7)
        self.vals = [None if i % 5 == 0 else rng.randrange(-10**6, 10**6)
                     for i in range(300)]
        self.data = cc.encode(self.vals)

    def test_directory_gap_rejected(self):
        # 把第二块的 offset 改大，制造目录空洞
        buf = bytearray(self.data)
        off, length, rc = struct.unpack_from(
            "<QIH", buf, cc.HEADER_SIZE + cc.DIRENT_SIZE)
        struct.pack_into("<QIH", buf, cc.HEADER_SIZE + cc.DIRENT_SIZE,
                         off + 8, length, rc)
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(buf))

    def test_crc_mismatch_rejected(self):
        buf = bytearray(self.data)
        buf[-5] ^= 0xFF  # 翻转载荷一字节，CRC 不变
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(buf))

    def test_truncation_rejected(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data[:-1])
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data[:cc.HEADER_SIZE - 1])

    def test_trailing_bytes_rejected(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data + b"\x00")

    def test_bad_magic_and_version(self):
        buf = bytearray(self.data)
        buf[0] ^= 0xFF
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(buf))
        buf = bytearray(self.data)
        buf[4] = 99
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(buf))

    def test_invalid_bit_width_rejected(self):
        vals = [1, 2, 3, 4]
        payload = bytearray(cc._payload_delta(vals))
        payload[8] = 66  # 非法位宽
        with self.assertRaises(cc.CodecError):
            cc._decode_delta(bytes(payload), len(vals))

    def test_delta_overflow_rejected(self):
        # INT64_MAX 再加正 delta，还原结果超出 int64
        payload = cc._payload_delta([cc.INT64_MAX - 1, cc.INT64_MAX])
        # 手工改成 INT64_MAX + 1：first=INT64_MAX, 位宽 2, ZigZag(+1)=2
        body = struct.pack("<q", cc.INT64_MAX) + bytes([2, 0b10])
        with self.assertRaises(cc.CodecError):
            cc._decode_delta(body, 2)
        self.assertEqual(cc._decode_delta(payload, 2),
                         [cc.INT64_MAX - 1, cc.INT64_MAX])

    def test_rle_length_mismatch_rejected(self):
        # 块 0 原本是 raw/delta/rle 之一；直接构造一个 RLE 长度不符的块
        bitmap = bytes(16)
        body = bytes([cc.ENC_RLE]) + bitmap + struct.pack("<qI", 5, 3)  # 只有 3 个值
        block = body + struct.pack("<I", zlib.crc32(body) & 0xFFFFFFFF)
        header = struct.pack("<4sBQI", cc.MAGIC, cc.VERSION, 128, 1)
        dirent = struct.pack("<QIH", cc.HEADER_SIZE + cc.DIRENT_SIZE,
                             len(block), 128)
        with self.assertRaises(cc.CodecError):
            cc.decode(header + dirent + block)

    def test_nonzero_bitmap_padding_rejected(self):
        vals = [1, None, 3]  # 3 行，位图占 1 字节，高 5 位为 padding
        data = cc.encode(vals)

        def flip(body):
            body[1] |= 0b10000000  # 置位未使用的尾位
        bad = mutate_block(data, 0, flip)
        with self.assertRaises(cc.CodecError):
            cc.decode(bad)

    def test_nonzero_delta_padding_rejected(self):
        vals = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9]  # delta 全为 1，位宽 1
        payload = bytearray(cc._payload_delta(vals))
        payload[-1] |= 0b10000000  # 打包区尾位置 1
        with self.assertRaises(cc.CodecError):
            cc._decode_delta(bytes(payload), len(vals))

    def test_limits_enforced(self):
        with self.assertRaises(cc.CodecError):
            cc.encode([0] * 11, max_rows=10)
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data, max_rows=10)
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data, max_bytes=len(self.data) - 1)

    def test_declared_rows_not_exceeded(self):
        # 目录行数被改小后与总行数不符，加载即拒绝
        buf = bytearray(self.data)
        struct.pack_into("<H", buf, cc.HEADER_SIZE + 8 + 4, 100)
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(buf))


class RangeTests(unittest.TestCase):
    def setUp(self):
        self.vals = [i * 3 - 100 if i % 3 else None for i in range(300)]
        self.data = cc.encode(self.vals)

    def test_empty_range_decodes_zero_blocks(self):
        for pos in (0, 128, 300):
            res = cc.read_range(self.data, pos, pos)
            self.assertEqual(res.values, [])
            self.assertEqual(res.blocks_decoded, 0)

    def test_out_of_bounds_rejected(self):
        for start, stop in [(-1, 5), (0, 301), (10, 5), (0, 301), (301, 301)]:
            with self.assertRaises(cc.CodecError):
                cc.read_range(self.data, start, stop)

    def test_full_range_equals_decode(self):
        res = cc.read_range(self.data, 0, 300)
        self.assertEqual(res.values, self.vals)
        self.assertEqual(res.blocks_decoded, 3)
        self.assertEqual(cc.decode(self.data), self.vals)

    def test_unread_block_crc_deferred(self):
        # 损坏第 2 块的 CRC：全量解码失败，但只读第 0 块的范围不受影响
        buf = bytearray(self.data)
        off1 = struct.unpack_from("<QIH", buf, cc.HEADER_SIZE + cc.DIRENT_SIZE)[0]
        buf[off1 + 1] ^= 0xFF
        bad = bytes(buf)
        with self.assertRaises(cc.CodecError):
            cc.decode(bad)
        res = cc.read_range(bad, 0, 100)
        self.assertEqual(res.values, self.vals[0:100])
        self.assertEqual(res.blocks_decoded, 1)


if __name__ == "__main__":
    unittest.main()
