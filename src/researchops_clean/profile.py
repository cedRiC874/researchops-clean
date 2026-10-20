"""Deterministic column profiles with a restricted model-facing projection."""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from . import csv_io
from .ops import is_number
from .sensitive_patterns import find_sensitive_types


SENTINEL_VALUES = (
    "999", "9999", "-1", "-99", "0000-00-00", "1900-01-01",
    "9999-12-31", "NA", "N/A", "NULL", "无", "不详", "未查",
)

# Each pattern limits the formats tried: strptime alone accepts some shorter
# strings even when the corresponding directive normally implies two digits.
DATE_FORMATS_BY_PATTERN: dict[str, list[str]] = {
    "99999999": ["%Y%m%d"],
}
for _separator in ("-", "/", "."):
    for _month in ("9", "99"):
        for _day in ("9", "99"):
            _date_pattern = f"9999{_separator}{_month}{_separator}{_day}"
            _date_format = f"%Y{_separator}%m{_separator}%d"
            DATE_FORMATS_BY_PATTERN[_date_pattern] = [_date_format]
            for _joiner in (" ", "T"):
                for _hour in ("9", "99"):
                    _time_pattern = f"{_date_pattern}{_joiner}{_hour}:99"
                    # ASCII letters, including the ISO separator T, become A.
                    _time_pattern = _time_pattern.replace("T", "A")
                    DATE_FORMATS_BY_PATTERN[_time_pattern] = [
                        f"{_date_format}{_joiner}%H:%M"
                    ]
                    DATE_FORMATS_BY_PATTERN[_time_pattern + ":99"] = [
                        f"{_date_format}{_joiner}%H:%M:%S"
                    ]
for _month in ("9", "99"):
    for _day in ("9", "99"):
        DATE_FORMATS_BY_PATTERN[f"9999汉{_month}汉{_day}汉"] = ["%Y年%m月%d日"]

_IDENTIFIER_WORDS = frozenset(
    ("id", "identifier", "patient", "participant", "subject", "email",
     "phone", "mobile", "name", "address")
)
_IDENTIFIER_CHINESE = (
    "姓名", "名字", "身份证", "证件", "住院号", "门诊号", "病历号", "就诊卡",
    "医保", "电话", "手机", "联系方式", "地址", "住址", "监护人", "父亲", "母亲",
    "家长",
)
_IDENTITY_TYPES = frozenset(("identity_number", "phone_number", "email"))
_INTEGER = re.compile(r"[+-]?[0-9]+")
_QUALIFIED_NUMBER = re.compile(r"(?:<=|>=|<|>|≤|≥)[+-]?[0-9]+(?:\.[0-9]+)?")
_AGE = re.compile(
    r"(?:[0-9]+岁)?(?:[0-9]+(?:个月|月))?(?:[0-9]+天)?"
)
_ABSOLUTE_PATH = re.compile(
    r"(?:^|[\s\"'(=])(?:[A-Za-z]:[\\/]|[\\/]{2}|/(?:[^\s/]|$)|~[\\/]|\.{1,2}[\\/])"
    r"|(?<=[：:；;，,、])/(?!/)[^\s/]+"
)
_PRIVACY_FULLWIDTH = str.maketrans(
    {character: character - 0xFEE0 for character in range(0xFF01, 0xFF5F)}
)
_HIDDEN_PATTERN = "[已隐藏模式]"
_HIDDEN_INTERNAL_VALUE = "[已隐藏路径或凭据]"


class ProfileError(ValueError):
    """A profile cannot be produced while preserving its privacy contract."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def format_pattern(text: str) -> str:
    """Replace digits, ASCII letters and Han characters with their markers."""
    result = []
    for character in text:
        codepoint = ord(character)
        if character.isdigit():
            result.append("9")
        elif "A" <= character <= "Z" or "a" <= character <= "z":
            result.append("A")
        elif (
            0x3400 <= codepoint <= 0x4DBF
            or 0x4E00 <= codepoint <= 0x9FFF
            or 0xF900 <= codepoint <= 0xFAFF
            or 0x20000 <= codepoint <= 0x323AF
        ):
            result.append("汉")
        else:
            result.append(character)
    return "".join(result)


def _has_path(text: str) -> bool:
    normalized = text.translate(_PRIVACY_FULLWIDTH).strip()
    return "file_path" in find_sensitive_types(normalized) or bool(
        _ABSOLUTE_PATH.search(normalized)
    )


def _unsafe_value(text: str) -> bool:
    normalized = text.translate(_PRIVACY_FULLWIDTH).strip()
    return bool(find_sensitive_types(text)) or bool(
        find_sensitive_types(normalized)
    ) or _has_path(text)


def _internal_value(text: str) -> str:
    if _has_path(text) or "api_key" in find_sensitive_types(
        text.translate(_PRIVACY_FULLWIDTH).strip()
    ):
        return _HIDDEN_INTERNAL_VALUE
    return text


def _frequencies(counter: Counter[str], key: str = "value") -> list[dict[str, Any]]:
    return [
        {key: value, "count": count}
        for value, count in sorted(counter.items(), key=lambda item: (-item[1], item[0]))
    ]


def _identifier_name(name: str) -> bool:
    if any(word in name for word in _IDENTIFIER_CHINESE):
        return True
    separated = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    separated = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", separated)
    words = {word.lower() for word in re.findall(r"[A-Za-z]+", separated)}
    return bool(words & _IDENTIFIER_WORDS)


def _date_parseable(text: str) -> bool:
    for date_format in DATE_FORMATS_BY_PATTERN.get(format_pattern(text), []):
        try:
            datetime.strptime(text, date_format)
        except ValueError:
            continue
        return True
    return False


def _serial_in_range(text: str) -> bool:
    if _INTEGER.fullmatch(text) is None:
        return False
    digits = text.lstrip("+").lstrip("0")
    # Avoid converting arbitrarily long cells to integers.
    return len(digits) == 5 and "20000" <= digits <= "60000"


def _kind(
    name: str, values: Counter[str], counts: dict[str, int], sensitive: bool
) -> str:
    non_empty = counts["non_empty"]
    if not non_empty:
        return "empty"
    if _identifier_name(name) or sensitive:
        return "identifier_like"
    if counts["age_text"] * 2 > non_empty:
        return "age_text_like"
    if (counts["date_parseable"] + counts["excel_serial_candidate"]) * 2 > non_empty:
        return "date_like"
    if counts["numeric_or_qualified"] * 2 > non_empty:
        if counts["numeric"] == non_empty:
            if (
                all(_INTEGER.fullmatch(value) for value in values)
                and len(values) <= 10
                and non_empty >= len(values) * 3
            ):
                return "code_like"
            return "numeric"
        return "numeric_with_tokens"
    average_length = sum(len(value) * count for value, count in values.items()) / non_empty
    if len(values) <= 20 and average_length <= 10:
        return "code_like"
    if non_empty >= 10 and len(values) / non_empty >= 0.9:
        return "identifier_like"
    return "text"


def _column_profiles(
    name: str, cells: list[str], k: int, source_values: set[str]
) -> tuple[dict[str, Any], dict[str, Any]]:
    values = Counter(value for value in cells if value != "")
    non_empty = sum(values.values())
    fullwidth_counts = {
        value: sum(0xFF01 <= ord(character) <= 0xFF5E or character == "\u3000"
                   for character in value)
        for value in values
    }
    date_values = {value for value in values if _date_parseable(value)}
    serial_values = {value for value in values if _serial_in_range(value)}
    date_count = sum(values[value] for value in date_values)
    serial_count = sum(values[value] for value in serial_values)
    remaining_count = non_empty - serial_count
    if not remaining_count or date_count * 2 <= remaining_count:
        serial_count = 0
    counts = {
        "non_empty": non_empty,
        "empty": len(cells) - non_empty,
        "distinct": len(values),
        "numeric": sum(count for value, count in values.items() if is_number(value)),
        "numeric_or_qualified": sum(
            count for value, count in values.items()
            if is_number(value) or _QUALIFIED_NUMBER.fullmatch(value)
        ),
        "leading_zero": sum(
            count for value, count in values.items()
            if len(value) > 1 and value.startswith("0") and re.fullmatch(r"[0-9]+", value)
        ),
        "excel_serial_candidate": serial_count,
        "sentinel_candidate": sum(values[value] for value in SENTINEL_VALUES),
        "fullwidth_characters": sum(
            fullwidth_counts[value] * count for value, count in values.items()
        ),
        "fullwidth_values": sum(
            count for value, count in values.items() if fullwidth_counts[value]
        ),
        "outer_whitespace_values": sum(
            count for value, count in values.items() if value != value.strip()
        ),
        "normalization_affected": sum(
            count for value, count in values.items()
            if fullwidth_counts[value] or value != value.strip()
        ),
        "age_text": sum(
            count for value, count in values.items() if _AGE.fullmatch(value)
        ),
        "date_parseable": date_count,
    }
    sensitive = any(
        _IDENTITY_TYPES.intersection(find_sensitive_types(value)) for value in values
    )
    kind = _kind(name, values, counts, sensitive)
    numeric_ratio = counts["numeric"] / non_empty if non_empty else 0
    candidate_values = Counter()
    value_key = None
    if kind == "code_like":
        candidate_values = values.copy()
        value_key = "values"
    elif kind == "numeric_with_tokens":
        candidate_values = Counter(
            {value: count for value, count in values.items() if not is_number(value)}
        )
        value_key = "non_numeric_values"
    allowed_values = Counter(
        {value: count for value, count in candidate_values.items()
         if count >= k and not _unsafe_value(value)}
    )
    # A pattern in one column must not reproduce an input value from another.
    withheld_values = source_values - allowed_values.keys()
    model_patterns: Counter[str] = Counter()
    internal_patterns: Counter[str] = Counter()
    internal_values: Counter[str] = Counter()
    internal_sentinels: Counter[str] = Counter()
    internal_non_numeric: Counter[str] = Counter()
    for value, count in values.items():
        pattern = format_pattern(value)
        safe_internal_value = _internal_value(value)
        internal_values[safe_internal_value] += count
        if value in SENTINEL_VALUES:
            internal_sentinels[safe_internal_value] += count
        if not is_number(value):
            internal_non_numeric[safe_internal_value] += count
        internal_patterns[
            _HIDDEN_PATTERN
            if _internal_value(pattern) == _HIDDEN_INTERNAL_VALUE
            else pattern
        ] += count
        # A format can itself be an identifier, path, or an unchanged rare
        # punctuation/emoji value. These cannot cross the model boundary.
        safe_pattern = (
            _HIDDEN_PATTERN
            if _unsafe_value(pattern) or pattern in withheld_values
            else pattern
        )
        model_patterns[safe_pattern] += count
    base = {
        "name": name,
        "kind": kind,
        "counts": counts,
        "numeric_ratio": numeric_ratio,
    }
    internal = {
        **base,
        "patterns": _frequencies(internal_patterns, "pattern")[:10],
        "values": _frequencies(internal_values),
        "sentinel_values": _frequencies(internal_sentinels),
    }
    if kind == "numeric_with_tokens":
        internal["non_numeric_values"] = _frequencies(internal_non_numeric)
    model = {
        **base,
        "counts": counts.copy(),
        "patterns": _frequencies(model_patterns, "pattern")[:10],
        "other_values": {
            "distinct": len(candidate_values) - len(allowed_values),
            "count": sum(candidate_values.values()) - sum(allowed_values.values()),
        },
    }
    if value_key:
        model[value_key] = _frequencies(allowed_values)
    return internal, model


def build_profiles(table: csv_io.Table, k: int = 5) -> tuple[dict[str, Any], dict[str, Any]]:
    """Build local and model projections without retaining rows or source paths."""
    if type(k) is not int or k < 5:
        raise ProfileError("profile_invalid_k", "隐私阈值 k 必须是至少为 5 的整数。")
    if any(_unsafe_value(name) for name in table.columns):
        raise ProfileError(
            "profile_header_sensitive", "CSV 列名包含敏感格式或路径，请先在本机修改列名。"
        )
    row_frequencies = Counter(
        tuple(row[column] for column in table.columns) for row in table.rows
    )
    row_summary = {
        "duplicate_row_count": sum(count for count in row_frequencies.values() if count > 1),
        "duplicate_group_count": sum(count > 1 for count in row_frequencies.values()),
    }
    input_summary = {
        "sha256": table.sha256,
        "columns": list(table.columns),
        "row_count": len(table.rows),
    }
    internal_columns = []
    model_columns = []
    source_values = {value for row in table.rows for value in row.values() if value}
    for name in table.columns:
        internal, model = _column_profiles(
            name, [row[name] for row in table.rows], k, source_values
        )
        internal_columns.append(internal)
        model_columns.append(model)
    internal_profile = {
        "schema_version": "cleaning-profile/internal/1",
        "input": input_summary,
        "row_summary": row_summary,
        "columns": internal_columns,
    }
    model_profile = {
        "schema_version": "cleaning-profile/model/1",
        "input": {**input_summary, "columns": list(table.columns)},
        "privacy": {"k": k},
        "row_summary": row_summary.copy(),
        "columns": model_columns,
    }
    return internal_profile, model_profile


def profile_csv(
    input_path: Path | str, *, k: int = 5, encoding: str = "utf-8-sig"
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Read the exact CSV bytes through the shared reader, then profile strings."""
    return build_profiles(csv_io.read_csv(Path(input_path), encoding=encoding), k=k)
