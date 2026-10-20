"""Read string-only CSV tables and serialize them reproducibly."""

from __future__ import annotations

import csv
import hashlib
import io
import threading
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path


MAX_INPUT_BYTES = 50 * 1024 * 1024
MAX_DATA_ROWS = 1_000_000
ALLOWED_ENCODINGS = ("utf-8-sig", "utf-8", "gbk", "gb18030")
_CSV_LIMIT_LOCK = threading.Lock()


class CSVError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass
class Table:
    columns: list[str]
    rows: list[dict[str, str]]
    sha256: str
    filename: str
    encoding: str


def _validate_columns(columns: Sequence[str]) -> None:
    if not columns or any(not isinstance(name, str) or not name.strip() for name in columns):
        raise CSVError("csv_header_invalid", "CSV 表头不能包含空列名。")
    positions = {}
    for position, name in enumerate(columns, 1):
        normalized = unicodedata.normalize("NFC", name)
        if normalized in positions:
            raise CSVError(
                "ambiguous_column_names",
                f"CSV 表头第 {positions[normalized]} 列和第 {position} 列的列名相同"
                "（可能只是看起来相同、编码不同），请先在原表中改名。",
            )
        positions[normalized] = position


def read_csv(path: Path, encoding: str = "utf-8-sig") -> Table:
    """Hash the original bytes; preserve every cell as a string."""

    if encoding not in ALLOWED_ENCODINGS:
        raise CSVError("encoding_not_allowed", "不支持的 CSV 编码。")
    path = Path(path)
    try:
        if path.stat().st_size > MAX_INPUT_BYTES:
            raise CSVError("input_too_large", "输入文件超过 50 MiB 限制。")
        with path.open("rb") as handle:
            raw = handle.read(MAX_INPUT_BYTES + 1)
    except OSError:
        raise CSVError("input_read_error", "无法读取输入 CSV 文件。") from None
    if len(raw) > MAX_INPUT_BYTES:
        raise CSVError("input_too_large", "输入文件超过 50 MiB 限制。")
    try:
        decoded = raw.decode(encoding)
    except UnicodeError:
        raise CSVError("input_decode_error", "输入文件无法按指定编码解码。") from None

    rows: list[dict[str, str]] = []
    # csv's default per-field cap is below the documented file-size limit.
    # The lock protects its process-wide setting when callers use threads.
    with _CSV_LIMIT_LOCK:
        previous_limit = csv.field_size_limit()
        try:
            csv.field_size_limit(MAX_INPUT_BYTES)
            reader = csv.reader(io.StringIO(decoded, newline=""), strict=True)
            columns = next(reader, [])
            _validate_columns(columns)
            for record in reader:
                if len(rows) >= MAX_DATA_ROWS:
                    raise CSVError("input_too_many_rows", "输入 CSV 超过 1,000,000 数据行限制。")
                if len(record) != len(columns):
                    raise CSVError("csv_row_invalid", "CSV 数据行的列数必须与表头一致。")
                rows.append(dict(zip(columns, record)))
        except csv.Error:
            raise CSVError("csv_invalid", "输入文件不是有效的 CSV。") from None
        finally:
            csv.field_size_limit(previous_limit)
    return Table(columns, rows, hashlib.sha256(raw).hexdigest(), path.name, encoding)


def csv_bytes(
    columns: Sequence[str], rows: Iterable[Mapping[str, str]]
) -> bytes:
    """Serialize explicit string cells as UTF-8 without BOM, using LF."""

    _validate_columns(columns)
    buffer = io.StringIO(newline="")
    # Include both CR and LF in csv's quoting rules, then replace only the
    # record terminator; embedded line breaks must retain their original value.
    writer = csv.writer(buffer, lineterminator="\r\n")

    def write_record(values: Sequence[str]) -> None:
        writer.writerow(values)
        buffer.seek(buffer.tell() - 2)
        buffer.write("\n")
        buffer.truncate()

    write_record(columns)
    expected = set(columns)
    for row in rows:
        if not isinstance(row, Mapping) or set(row) != expected:
            raise CSVError("csv_row_invalid", "CSV 数据行的列名必须与表头一致。")
        values = [row[column] for column in columns]
        if any(not isinstance(value, str) for value in values):
            raise CSVError("csv_value_invalid", "CSV 单元格必须是字符串。")
        write_record(values)
    try:
        return buffer.getvalue().encode("utf-8")
    except UnicodeError:
        raise CSVError("csv_value_invalid", "CSV 单元格包含无效的 Unicode 字符。") from None


def write_csv(
    path: Path, columns: Sequence[str], rows: Iterable[Mapping[str, str]]
) -> None:
    payload = csv_bytes(columns, rows)
    try:
        Path(path).write_bytes(payload)
    except OSError:
        raise CSVError("output_write_error", "无法写入输出 CSV 文件。") from None
