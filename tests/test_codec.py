import random
import struct
import unittest
import zlib

import column_codec as cc

INT64_MIN = cc.INT64_MIN
INT64_MAX = cc.INT64_MAX


def corrupt(data: bytes, offset: int, xor: int = 0xFF) -> bytes:
    b = bytearray(data)
    b[offset] ^= xor
    return bytes(b)


def build_single_block_file(rows, enc_id, payload) -> bytes:
    """手工组装单块文件（用于强制指定编码的用例）。"""
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


class RoundTripTests(unittest.TestCase):
    def check(self, values):
        data = cc.encode(values)
        self.assertEqual(cc.decode(data), values)
        n = len(values)
        if n <= 40:
            ranges = [(s, t) for s in range(n + 1) for t in range(s, n + 1)]
        else:
            ranges = [(0, n), (0, 1), (n - 1, n), (n, n),
                      (0, 128), (120, 136), (128, n), (7, 7), (3, 200)]
            ranges = [(s, t) for s, t in ranges if 0 <= s <= t <= n]
        for start, stop in ranges:
            got, _ = cc.read_range(data, start, stop)
            self.assertEqual(got, values[start:stop], (start, stop))
        return data

    def test_short_reference_list(self):
        self.check([1, None, -2, INT64_MAX, None, INT64_MIN, 0, 7])

    def test_empty_column(self):
        data = cc.encode([])
        self.assertEqual(cc.decode(data), [])
        got, n = cc.read_range(data, 0, 0)
        self.assertEqual((got, n), ([], 0))

    def test_int64_extremes_alternating(self):
        values = [INT64_MIN, INT64_MAX] * 64  # 128 行，int64 两端交替
        data = self.check(values)
        # 65 位 delta 比 raw 的 64 位更占字节，按最短原则应选 raw
        enc_off = cc._HEADER.size + cc._DIRENT.size
        self.assertEqual(data[enc_off], cc.ENC_RAW)

    def test_none_interspersed_rle(self):
        # 非空序列呈长游程且相邻游程差很大：RLE 应短于 raw 与 delta
        values = []
        for _ in range(8):
            values.extend([2**40] * 5)
            values.append(None)
            values.extend([7] * 5)
            values.append(None)
        data = self.check(values)
        enc_off = cc._HEADER.size + cc._DIRENT.size
        self.assertEqual(data[enc_off], cc.ENC_RLE)

    def test_all_null_block(self):
        self.check([None] * 128)

    def test_constant_block(self):
        self.check([42] * 128)

    def test_single_row(self):
        self.check([None])
        self.check([INT64_MIN])

    def test_block_boundary_128(self):
        values = list(range(200))  # 128 + 72
        data = self.check(values)
        self.assertEqual(len(data) and cc.decode(data), values)
        # 跨块范围只解码相交的 2 块
        got, n = cc.read_range(data, 120, 136)
        self.assertEqual(got, values[120:136])
        self.assertEqual(n, 2)
        # 单块范围只解码 1 块
        got, n = cc.read_range(data, 0, 128)
        self.assertEqual((got, n), (values[:128], 1))
        got, n = cc.read_range(data, 128, 200)
        self.assertEqual((got, n), (values[128:], 1))

    def test_65bit_delta(self):
        # int64 两端相邻非空值的 delta 经 ZigZag 后需要 65 位
        values = [INT64_MIN, INT64_MAX, INT64_MIN]
        payload = cc._delta_payload(values)
        self.assertEqual(payload[8], 65)  # 首值 8 字节之后是位宽
        data = build_single_block_file(values, cc.ENC_DELTA, payload)
        self.assertEqual(cc.decode(data), values)
        got, n = cc.read_range(data, 1, 3)
        self.assertEqual((got, n), ([INT64_MAX, INT64_MIN], 1))

    def test_random_roundtrip_and_ranges(self):
        rng = random.Random(20260920)
        for _ in range(20):
            n = rng.randrange(0, 300)
            values = []
            for _ in range(n):
                r = rng.random()
                if r < 0.25:
                    values.append(None)
                elif r < 0.5:
                    values.append(rng.choice([INT64_MIN, INT64_MAX, 0, -1, 1]))
                elif r < 0.75:
                    values.append(rng.randrange(-5, 6))
                else:
                    values.append(rng.randrange(INT64_MIN, INT64_MAX + 1))
            data = cc.encode(values)
            self.assertEqual(cc.decode(data), values)
            for _ in range(10):
                start = rng.randrange(0, n + 1)
                stop = rng.randrange(start, n + 1)
                got, cnt = cc.read_range(data, start, stop)
                self.assertEqual(got, values[start:stop])
                if start == stop:
                    self.assertEqual(cnt, 0)

    def test_encoding_tie_break_order(self):
        # 常数：delta 位宽 0，9 字节载荷，短于 RLE 的 12 字节
        self.assertEqual(cc._encode_block([5] * 3)[0], cc.ENC_DELTA)
        self.assertEqual(cc._encode_block([1, 2, 3])[0], cc.ENC_DELTA)
        # 65 位 delta 比 raw 更长，选 raw
        self.assertEqual(cc._encode_block([INT64_MIN, 0, INT64_MAX])[0], cc.ENC_RAW)
        # raw 与 delta 等长（各 16 字节载荷）时平局，按顺序选 raw
        self.assertEqual(cc._encode_block([0, 2**48])[0], cc.ENC_RAW)
        # 长游程且游程间差很大时 RLE 最短
        rows = ([2**40] * 20 + [7] * 20) * 3
        self.assertEqual(cc._encode_block(rows)[0], cc.ENC_RLE)


class RangeValidationTests(unittest.TestCase):
    def setUp(self):
        self.values = list(range(10))
        self.data = cc.encode(self.values)

    def test_empty_range_decodes_zero_blocks(self):
        for p in (0, 5, 10):
            got, n = cc.read_range(self.data, p, p)
            self.assertEqual((got, n), ([], 0))

    def test_out_of_range(self):
        for start, stop in [(-1, 5), (0, 11), (6, 5), (0, 100), (11, 11)]:
            with self.assertRaises(cc.CodecError):
                cc.read_range(self.data, start, stop)

    def test_max_rows_limit(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data, max_rows=5)
        with self.assertRaises(cc.CodecError):
            cc.encode(list(range(100)), max_rows=10)

    def test_max_bytes_limit(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data, max_bytes=len(self.data) - 1)


class CorruptionTests(unittest.TestCase):
    def setUp(self):
        self.values = [i if i % 3 else None for i in range(200)]
        self.data = cc.encode(self.values)
        self.nblocks = 2
        self.dir_end = cc._HEADER.size + self.nblocks * cc._DIRENT.size

    def test_bad_magic(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(corrupt(self.data, 0))

    def test_bad_version(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(corrupt(self.data, 4))

    def test_reserved_padding_nonzero(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(corrupt(self.data, 5))

    def test_truncation(self):
        for cut in (1, cc._HEADER.size - 1, self.dir_end - 1, len(self.data) - 1):
            with self.assertRaises(cc.CodecError, msg=str(cut)):
                cc.decode(self.data[:cut])

    def test_trailing_bytes(self):
        with self.assertRaises(cc.CodecError):
            cc.decode(self.data + b"\x00")

    def test_directory_hole(self):
        # 把第二个目录项的 offset 加 8，制造空洞
        b = bytearray(self.data)
        off_pos = cc._HEADER.size + cc._DIRENT.size
        off = struct.unpack_from("<Q", b, off_pos)[0]
        struct.pack_into("<Q", b, off_pos, off + 8)
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(b))

    def test_directory_row_count_mismatch(self):
        b = bytearray(self.data)
        struct.pack_into("<I", b, cc._HEADER.size + 12, 100)  # 首块行数 128 -> 100
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(b))

    def test_crc_failure(self):
        # 翻转载荷区一个字节（首块内部，跳过目录）
        with self.assertRaises(cc.CodecError):
            cc.decode(corrupt(self.data, self.dir_end + 3))

    def test_unread_block_crc_deferred(self):
        # 损坏第二块，只读第一块范围不应报错
        bad = corrupt(self.data, len(self.data) - 3)
        got, n = cc.read_range(bad, 0, 10)
        self.assertEqual((got, n), (self.values[:10], 1))
        with self.assertRaises(cc.CodecError):
            cc.read_range(bad, 150, 160)
        with self.assertRaises(cc.CodecError):
            cc.decode(bad)

    def test_bad_encoding_id(self):
        b = bytearray(self.data)
        b[self.dir_end] = 99
        # 修正 CRC 以隔离编码标识错误
        rows = 128
        bm = rows // 8
        blk_len = struct.unpack_from("<I", self.data, cc._HEADER.size + 8)[0]
        body = bytes(b[self.dir_end:self.dir_end + blk_len - 4])
        struct.pack_into("<I", b, self.dir_end + blk_len - 4, zlib.crc32(body))
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(b))

    def test_illegal_delta_width(self):
        # 手工组装 delta 块并把位宽改为 66（重算 CRC 以隔离位宽错误）
        values = [INT64_MIN, INT64_MAX]
        payload = bytearray(cc._delta_payload(values))
        payload[8] = 66
        data = build_single_block_file(values, cc.ENC_DELTA, bytes(payload))
        with self.assertRaises(cc.CodecError):
            cc.decode(data)

    def test_bitmap_padding_nonzero(self):
        # 130 行：第二块 2 行，位图 1 字节，高 6 位为 padding
        values = list(range(130))
        data = cc.encode(values)
        b = bytearray(data)
        off0, len0 = struct.unpack_from("<QI", data, cc._HEADER.size)
        second = off0 + len0
        b[second + 1] |= 0x80  # 位图 padding 置位
        blk_len = struct.unpack_from("<I", data, cc._HEADER.size + cc._DIRENT.size + 8)[0]
        body = bytes(b[second:second + blk_len - 4])
        struct.pack_into("<I", b, second + blk_len - 4, zlib.crc32(body))
        with self.assertRaises(cc.CodecError):
            cc.decode(bytes(b))

    def test_rle_length_mismatch(self):
        # 手工组装 RLE 块，篡改游程 count 使总和与非空数不符
        values = [9] * 128
        payload = bytearray(cc._rle_payload([9] * 128))
        struct.pack_into("<I", payload, 8, 100)  # count 128 -> 100
        data = build_single_block_file(values, cc.ENC_RLE, bytes(payload))
        with self.assertRaises(cc.CodecError):
            cc.decode(data)

    def test_value_out_of_int64(self):
        with self.assertRaises(cc.CodecError):
            cc.encode([INT64_MAX + 1])
        with self.assertRaises(cc.CodecError):
            cc.encode([INT64_MIN - 1])
        with self.assertRaises(cc.CodecError):
            cc.encode([True])
        with self.assertRaises(cc.CodecError):
            cc.encode([1.5])


if __name__ == "__main__":
    unittest.main()
