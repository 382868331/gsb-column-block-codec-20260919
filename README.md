# 可随机定位的列式整数块编码库

把 `int64 | None` 序列编码成连续的数据块文件，支持只解码查询范围相交的块。
纯 Python 3.14.7 标准库实现，Windows 原生离线运行，无第三方依赖，不兼容 Parquet。

## 运行

```bat
python demo.py                                  :: 约 1 秒：正常结果 + 一次真实触发的 CRC 失败
python -m unittest discover -s tests -v         :: 29 个测试
```

## 接口（`column_codec.py`）

- `encode(values, *, max_rows=DEFAULT_MAX_ROWS) -> bytes`
  编码 `int | None` 序列。值必须在 int64 范围内，否则抛 `CodecError`。
- `decode(data, *, max_rows=..., max_bytes=...) -> list`
  全量解码，返回长度恰为声明总行数的列表。
- `read_range(data, start, stop, *, max_rows=..., max_bytes=...) -> (list, int)`
  解码 `[start, stop)`，要求 `0 <= start <= stop <= 总行数`；只解码相交块，
  返回 `(行值列表, 实际解码块数)`；空范围解码 0 块。
- 所有格式/校验/边界错误抛出 `CodecError`（`ValueError` 子类）。

加载（构造）时即校验：magic、版本、保留字节全零、块数量与总行数一致、
目录连续无空洞无重叠、非末块恰 128 行、目录行数之和等于声明总行数、
无截断无尾随字节；声明行数与输入字节数不得超过 `max_rows` / `max_bytes`。
每块 CRC32 在该块首次被解码时校验，未读块校验延迟。

## 二进制格式（多字节字段一律小端）

```
文件   = 文件头 + 目录[block_count] + 块[0..block_count)      -- 块连续无空洞
文件头 = magic "CIB1"(4) + version u8(=1) + reserved(3, 全零)
         + total_rows u64 + block_count u32                    -- 共 20 字节
目录项 = offset u64 + length u32 + row_count u32               -- 共 16 字节
         offset 为绝对偏移；entry[0].offset = 20 + 16*block_count；
         entry[i].offset + entry[i].length = entry[i+1].offset；
         最后一项末尾必须恰为文件末尾
块     = encoding u8 + null_bitmap(ceil(row_count/8) 字节)
         + payload + crc32 u32
```

约束：`row_count` 非末块必须为 128，末块 1..128；`total_rows = 0` 时
`block_count = 0`。`null_bitmap` 第 i 位置 1 表示第 i 行为 None（低位在前），
末尾未用位必须为 0。`crc32`（zlib CRC32）覆盖 `encoding + null_bitmap + payload`。

### 非空值编码（encoding 标识）

按完整块实际字节数选择最短，平局按 raw → delta → rle 顺序。

- **0 raw**：`non_null_count` 个 int64 小端依次排列。
- **1 delta**：首个非空值 int64（`non_null_count = 0` 即全空块时不存首值），
  随后当非空值 ≥ 2 时为 `width u8` + 统一位宽打包的 ZigZag delta 流
  （不足 2 个非空值时无 delta 项，也无位宽字节）。
  ZigZag(d) = 2d（d ≥ 0），否则 -2d-1；位宽取实际最大值的最小位宽，合法 0..65；
  位流高位在前，末尾未用位补 0。还原的每个值必须在 int64 范围内。
- **2 rle**：若干 `(value int64, count u32)` 游程，count ≥ 1，
  所有 count 之和必须等于 `non_null_count`。

### 拒绝情形

截断、尾随字节、目录空洞/重叠、行数不符、非法编码标识、非法位宽（>65）、
坏 CRC32、RLE 长度不符、位图/位流/文件头非零 padding、delta 还原值溢出 int64、
范围越界、超过构造上限。

## 文件

- `column_codec.py` — 库本体
- `demo.py` — 演示（正常往返、128 行边界、65 位 delta、真实 CRC 失败）
- `tests/test_codec.py` — 单元测试（固定种子随机往返 + 各类损坏注入）
