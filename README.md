# 可随机定位的列式整数块编码库

把一列可空 int64（`int` 或 `None`）编码成带目录的数据块，支持只解码查询范围涉及的块。纯 Python 3.14 标准库实现，Windows 原生离线运行，无第三方依赖，格式不兼容 Parquet。

## 接口（`column_codec.py`）

- `encode(values, *, max_rows=MAX_ROWS) -> bytes`：编码 int/None 序列。
- `decode(data, *, max_rows=..., max_bytes=...) -> list`：全量解码。
- `read_range(data, start, stop, ...) -> RangeResult`：只解码与 `[start, stop)` 相交的块，返回 `RangeResult(values, blocks_decoded)`；要求 `0 <= start <= stop <= 总行数`，空范围解码 0 块。
- 所有非法输入与损坏数据抛出 `CodecError`（`ValueError` 子类）。
- 构造期限定：`MAX_ROWS = 2**26` 行、`MAX_INPUT_BYTES = 2**26` 字节，可用关键字参数收紧。

## 二进制格式（多字节字段一律小端序）

```
文件头（17 字节）
    magic       4s   b"CIB1"
    version     B    1
    total_rows  Q    总行数
    block_count I    块数 = ceil(total_rows / 128)
块目录（block_count 项，每项 14 字节，必须连续无空洞）
    offset      Q    块记录相对文件起始的偏移
    length      I    块记录字节数（含 CRC）
    row_count   H    本块行数（每块最多 128 行，仅末块可不足）
块记录（紧跟目录，首尾相接，末尾不得有残余字节）
    encoding    B    0=raw 1=delta 2=rle
    null_bitmap ceil(row_count/8) 字节，bit i 置位表示第 i 行为 NULL，
                LSB 优先，未使用尾位必须为 0
    payload     依编码而定（见下）
    crc32       I    覆盖 encoding + null_bitmap + payload
```

非空值按行序取出后，三种 payload：

- **raw**：`nnz` 个 int64。
- **delta**：首个非空值 int64；非空值 ≥ 2 时追加位宽字节 `B`（0..65）和 `(nnz-1)` 个 ZigZag delta 的统一位宽打包（LSB 优先，未使用尾位为 0）。非空值不足 2 时无 delta 项，全空块不存首值。`ZigZag(d) = 2d`（d≥0）否则 `-2d-1`，位宽取最小值；还原值必须在 int64 内。
- **rle**：若干 `(value int64, run_length uint32)` 对，`run_length >= 1`，总长必须等于非空值个数。

编码时按完整块实际字节数选最短编码，平局按 raw → delta → rle 顺序。

## 校验

加载时校验 magic、版本、块数与总行数一致、目录连续无空洞、边界与行数；读取时只解码相交块并记录块数，每块校验 CRC32、位图与打包区尾位、位宽（≤65）、RLE 长度匹配、delta 还原不溢出 int64。未读块的校验延迟到读取时。

## 运行

```
python demo.py                              # 演示：正常往返 + 实际触发的 CRC 失败
python -m unittest discover -s tests -v     # 25 个单元测试
```

测试覆盖：int64 两端交替、None 间隔 RLE、128 行边界、全空/常数/单行、65 位 delta、目录空洞、CRC 损坏、截断、非法位宽、非零 padding、RLE 长度不符、空范围与越界、固定种子随机往返与随机范围。
