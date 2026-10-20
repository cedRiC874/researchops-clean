"""Versioned, deterministic cleaning operations over string-valued CSV cells."""

from __future__ import annotations

import copy
import re
import unicodedata
from datetime import datetime, timedelta
from decimal import Decimal, localcontext
from typing import Any

from jsonschema import Draft202012Validator


class OperationError(ValueError):
    """An operation was invalid or could not finish deterministically."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_NUMBER = re.compile(r"[+-]?[0-9]+(?:\.[0-9]+)?")
_INTEGER = re.compile(r"[+-]?[0-9]+")
_AGE = re.compile(
    r"(?:(?P<years>[0-9]+)岁)?"
    r"(?:(?P<months>[0-9]+)(?:个月|月))?"
    r"(?:(?P<days>[0-9]+)天)?"
)
_LAB_NUMBER = re.compile(r"(?P<qualifier><=|>=|<|>|≤|≥)(?P<number>[+-]?[0-9]+(?:\.[0-9]+)?)")
_FULLWIDTH = str.maketrans(
    {character: character - 0xFEE0 for character in range(0xFF01, 0xFF5F)}
)


def is_number(text: str) -> bool:
    """Recognize ASCII decimal notation, without whitespace or exponents."""
    return isinstance(text, str) and _NUMBER.fullmatch(text) is not None


def decimal_text(number: Decimal) -> str:
    """Render an exact finite Decimal without an exponent or redundant zeros."""
    if not number.is_finite():
        raise OperationError("recipe_invalid", "数值必须是有限十进制数。")
    if number.is_zero():
        return "0"
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    return result


def _object(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties) if required is None else required,
        "additionalProperties": False,
    }


_COLUMN = {"type": "string", "minLength": 1, "pattern": r"\S"}
_STRING = {"type": "string"}
_NUMBER_SCHEMA = {"type": "string", "pattern": r"^[+-]?[0-9]+(\.[0-9]+)?$"}
_STRINGS = {"type": "array", "items": _STRING, "uniqueItems": True}
_WHERE = _object(
    {
        "column": _COLUMN,
        "equals": _STRING,
        "in": {"type": "array", "items": _STRING, "minItems": 1, "uniqueItems": True},
    },
    ["column"],
)
_WHERE["oneOf"] = [{"required": ["equals"]}, {"required": ["in"]}]

_SCHEMAS = {
    "normalize_text": _object({"column": _COLUMN}),
    "sentinel_to_missing": _object({"column": _COLUMN, "values": _STRINGS}),
    "map_values": _object(
        {
            "column": _COLUMN,
            "mapping": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": {"type": "string", "minLength": 1},
            },
            "on_unmapped": {"enum": ["keep", "keep_and_flag", "error"]},
        }
    ),
    "parse_date": _object(
        {
            "column": _COLUMN,
            "output_column": _COLUMN,
            "formats": {
                "type": "array",
                "items": {"type": "string", "minLength": 1},
                "minItems": 1,
                "uniqueItems": True,
            },
            "output": {"enum": ["date", "datetime"]},
            "excel_serial": _object(
                {
                    "enabled": {"type": "boolean"},
                    "min": {"type": "integer"},
                    "max": {"type": "integer"},
                }
            ),
        }
    ),
    "parse_age_cn": _object(
        {
            "column": _COLUMN,
            "outputs": _object({"years": _COLUMN, "months": _COLUMN, "days": _COLUMN}),
        }
    ),
    "parse_lab_result": _object(
        {
            "column": _COLUMN,
            "qualitative_values": _STRINGS,
            "invalid_values": _STRINGS,
            "outputs": _object(
                {
                    "value": _COLUMN,
                    "qualifier": _COLUMN,
                    "qualitative": _COLUMN,
                    "invalid": _COLUMN,
                }
            ),
        }
    ),
    "convert_unit": _object(
        {
            "value_column": _COLUMN,
            "unit_column": _COLUMN,
            "conversions": {
                "type": "array",
                "minItems": 1,
                "items": _object({"from": _COLUMN, "to": _COLUMN, "factor": _NUMBER_SCHEMA}),
            },
            "where": _WHERE,
        },
        ["value_column", "unit_column", "conversions"],
    ),
    "range_check": _object(
        {"column": _COLUMN, "min": _NUMBER_SCHEMA, "max": _NUMBER_SCHEMA, "where": _WHERE},
        ["column", "min", "max"],
    ),
    "flag_duplicates": _object(
        {"key_columns": {"type": "array", "items": _COLUMN, "minItems": 1, "uniqueItems": True}}
    ),
}


def operation_catalog() -> list[dict[str, Any]]:
    """Return independent JSON schemas in stable operation order."""
    return [
        {"op": name, "op_version": "1", "params_schema": copy.deepcopy(schema)}
        for name, schema in _SCHEMAS.items()
    ]


def declared_output_columns(op: str, params: Any) -> list[str]:
    """Extract usable declarations even from a partially drafted step."""
    if not isinstance(params, dict):
        return []
    if op == "parse_date":
        candidates = [params.get("output_column")]
    elif op in ("parse_age_cn", "parse_lab_result"):
        outputs = params.get("outputs")
        if not isinstance(outputs, dict):
            return []
        keys = (
            ("years", "months", "days")
            if op == "parse_age_cn"
            else ("value", "qualifier", "qualitative", "invalid")
        )
        candidates = [outputs.get(key) for key in keys]
    else:
        return []
    return [value for value in candidates if isinstance(value, str) and value.strip()]


def validate_operation(op: str, params: Any, columns: list[str]) -> list[str]:
    """Validate a step and return appended columns without mutating its input."""
    if not isinstance(op, str) or op not in _SCHEMAS:
        raise OperationError("recipe_invalid", "配方包含未知操作。")
    if next(Draft202012Validator(_SCHEMAS[op]).iter_errors(params), None) is not None:
        raise OperationError("recipe_invalid", f"操作 {op} 的参数不符合 schema。")

    references = []
    if "column" in params:
        references.append(params["column"])
    if op == "convert_unit":
        references.extend([params["value_column"], params["unit_column"]])
        if params["value_column"] == params["unit_column"]:
            raise OperationError("recipe_invalid", "数值列和单位列必须不同。")
        conversions = params["conversions"]
        source_units = [item["from"] for item in conversions]
        target_units = {item["to"] for item in conversions}
        if len(source_units) != len(set(source_units)) or set(source_units) & target_units:
            raise OperationError("recipe_invalid", "换算来源单位不能重复，也不能是目标单位。")
        if any(not is_number(item["factor"]) for item in conversions):
            raise OperationError("recipe_invalid", "换算系数必须是 ASCII 十进制数字字符串。")
    if op == "range_check":
        if not is_number(params["min"]) or not is_number(params["max"]):
            raise OperationError("recipe_invalid", "范围边界必须是 ASCII 十进制数字字符串。")
        if Decimal(params["min"]) > Decimal(params["max"]):
            raise OperationError("recipe_invalid", "范围下限不能大于上限。")
    if op == "parse_date" and params["excel_serial"]["min"] > params["excel_serial"]["max"]:
        raise OperationError("recipe_invalid", "Excel 序列日期下限不能大于上限。")
    if op == "parse_lab_result":
        if set(params["qualitative_values"]) & set(params["invalid_values"]):
            raise OperationError("recipe_invalid", "定性值列表与无效值列表不能重叠。")
    if op == "flag_duplicates":
        references.extend(params["key_columns"])
    if "where" in params:
        references.append(params["where"]["column"])
    if any(column not in columns for column in references):
        raise OperationError("column_missing", "操作引用了不存在的列。")

    new_columns = declared_output_columns(op, params)
    normalized_new = [unicodedata.normalize("NFC", column) for column in new_columns]
    normalized_existing = {unicodedata.normalize("NFC", column) for column in columns}
    if len(normalized_new) != len(set(normalized_new)) or any(
        column in normalized_existing for column in normalized_new
    ):
        raise OperationError("recipe_invalid", "新列名必须互不重复，且不能覆盖已有列。")
    return new_columns


def _matches_where(row: dict[str, str], params: dict[str, Any]) -> bool:
    where = params.get("where")
    if where is None:
        return True
    value = row[where["column"]]
    return value == where["equals"] if "equals" in where else value in where["in"]


def _parse_date(value: str, params: dict[str, Any]) -> set[str]:
    parsed_values = []
    for date_format in params["formats"]:
        try:
            parsed_values.append(datetime.strptime(value, date_format))
        except (ValueError, OverflowError):
            pass
    serial = params["excel_serial"]
    if serial["enabled"] and _INTEGER.fullmatch(value):
        # Decimal comparison avoids int() limits for an arbitrarily long input.
        serial_value = Decimal(value)
        if Decimal(serial["min"]) <= serial_value <= Decimal(serial["max"]):
            try:
                parsed_values.append(datetime(1899, 12, 30) + timedelta(days=int(serial_value)))
            except (OverflowError, ValueError):
                pass
    if params["output"] == "date":
        return {parsed.date().isoformat() for parsed in parsed_values}
    return {
        f"{parsed.year:04d}-{parsed.month:02d}-{parsed.day:02d}T{parsed.hour:02d}:{parsed.minute:02d}"
        for parsed in parsed_values
    }


def execute_operation(
    op: str,
    params: dict[str, Any],
    columns: list[str],
    rows: list[dict[str, str]],
    step_id: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Execute one validated operation, recording changed cells and flags."""
    new_columns = validate_operation(op, params, columns)
    changes: list[dict[str, Any]] = []
    flags: list[dict[str, Any]] = []
    columns.extend(new_columns)
    for row in rows:
        for column in new_columns:
            row[column] = ""

    def change(row_number: int, row: dict[str, str], column: str, value: str) -> None:
        old_value = row[column]
        if old_value == value:
            return
        row[column] = value
        changes.append(
            {
                "row": row_number,
                "column": column,
                "old_value": old_value,
                "new_value": value,
                "step_id": step_id,
                "op": op,
            }
        )

    def flag(row_number: int, column: str, code: str, detail: str) -> None:
        flags.append(
            {
                "row": row_number,
                "column": column,
                "flag_code": code,
                "step_id": step_id,
                "detail": detail,
            }
        )

    if op == "flag_duplicates":
        groups: dict[tuple[str, ...], list[int]] = {}
        for row_number, row in enumerate(rows, 1):
            key = tuple(row[column] for column in params["key_columns"])
            groups.setdefault(key, []).append(row_number)
        duplicate_groups: dict[int, str] = {}
        group_number = 0
        for row_numbers in groups.values():
            if len(row_numbers) > 1:
                group_number += 1
                for row_number in row_numbers:
                    duplicate_groups[row_number] = f"group_{group_number}"
        for row_number in sorted(duplicate_groups):
            for column in params["key_columns"]:
                flag(row_number, column, "duplicate_key", duplicate_groups[row_number])
        return changes, flags

    for row_number, row in enumerate(rows, 1):
        if not _matches_where(row, params):
            continue
        column = params.get("column", params.get("value_column"))
        value = row[column]
        if op == "normalize_text":
            change(row_number, row, column, value.translate(_FULLWIDTH).strip())
        elif op == "sentinel_to_missing":
            if value and value in params["values"]:
                change(row_number, row, column, "")
                flag(row_number, column, "sentinel_missing", value)
        elif not value:
            continue
        elif op == "map_values":
            if value in params["mapping"]:
                change(row_number, row, column, params["mapping"][value])
            elif params["on_unmapped"] == "keep_and_flag":
                flag(row_number, column, "unmapped_value", value)
            elif params["on_unmapped"] == "error":
                raise OperationError("unmapped_value", "存在未映射的非空值，配方执行失败。")
        elif op == "parse_date":
            parsed = _parse_date(value, params)
            if len(parsed) == 1:
                change(row_number, row, params["output_column"], next(iter(parsed)))
            else:
                flag(row_number, column, "ambiguous_date" if parsed else "unparsed_date", value)
        elif op == "parse_age_cn":
            match = _AGE.fullmatch(value)
            if match is None:
                flag(row_number, column, "unparsed_age", value)
            else:
                for component in ("years", "months", "days"):
                    parsed = (match.group(component) or "0").lstrip("0") or "0"
                    change(row_number, row, params["outputs"][component], parsed)
        elif op == "parse_lab_result":
            outputs = params["outputs"]
            match = _LAB_NUMBER.fullmatch(value)
            if is_number(value):
                change(row_number, row, outputs["value"], decimal_text(Decimal(value)))
            elif match is not None:
                qualifier = {"≤": "<=", "≥": ">="}.get(match["qualifier"], match["qualifier"])
                change(row_number, row, outputs["value"], decimal_text(Decimal(match["number"])))
                change(row_number, row, outputs["qualifier"], qualifier)
            elif value in params["qualitative_values"]:
                change(row_number, row, outputs["qualitative"], value)
            elif value in params["invalid_values"]:
                change(row_number, row, outputs["invalid"], "1")
            else:
                flag(row_number, column, "unparsed_result", value)
        elif op == "convert_unit":
            unit_column = params["unit_column"]
            unit = row[unit_column]
            conversions = {item["from"]: item for item in params["conversions"]}
            target_units = {item["to"] for item in params["conversions"]}
            if not unit:
                flag(row_number, unit_column, "missing_unit", value)
            elif unit not in conversions and unit not in target_units:
                flag(row_number, unit_column, "unknown_unit", unit)
            elif not is_number(value):
                flag(row_number, column, "not_numeric", value)
            elif unit in conversions:
                conversion = conversions[unit]
                number, factor = Decimal(value), Decimal(conversion["factor"])
                with localcontext() as context:
                    context.prec = max(1, len(number.as_tuple().digits) + len(factor.as_tuple().digits))
                    converted = number * factor
                change(row_number, row, column, decimal_text(converted))
                change(row_number, row, unit_column, conversion["to"])
        elif op == "range_check":
            if is_number(value) and not Decimal(params["min"]) <= Decimal(value) <= Decimal(params["max"]):
                flag(row_number, column, "out_of_range", value)
    return changes, flags
