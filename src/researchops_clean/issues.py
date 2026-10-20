"""Deterministic pending cleaning suggestions from the public profile only."""

from __future__ import annotations

import copy
import math
import re
from typing import Any

from . import ops
from .profile import DATE_FORMATS_BY_PATTERN, SENTINEL_VALUES, _unsafe_value
from .recipe import RecipeError, validate_recipe


class IssueError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_KINDS = {
    "empty", "identifier_like", "age_text_like", "date_like",
    "numeric", "numeric_with_tokens", "code_like", "text",
}
_COUNT_FIELDS = (
    "non_empty", "empty", "distinct", "numeric", "numeric_or_qualified",
    "leading_zero", "excel_serial_candidate", "sentinel_candidate",
    "fullwidth_characters", "fullwidth_values", "outer_whitespace_values",
    "normalization_affected", "age_text", "date_parseable",
)
_QUALITATIVE_VALUES = ("阴性", "阳性", "+", "++", "+++", "弱阳性")
_INVALID_VALUES = ("溶血", "标本不合格", "脂血", "凝块")
_QUALIFIED_NUMBER = re.compile(r"(?:<=|>=|<|>|≤|≥)[+-]?[0-9]+(?:\.[0-9]+)?")
_FULLWIDTH = str.maketrans(
    {character: character - 0xFEE0 for character in range(0xFF01, 0xFF5F)}
)
_ASCII_LOWER = str.maketrans(
    {character: character + 32 for character in range(ord("A"), ord("Z") + 1)}
)
_ALIAS_GROUPS = (
    frozenset(("男", "男性", "m")),
    frozenset(("女", "女性", "f")),
    frozenset(("hgb", "hb", "血红蛋白")),
)


def _integer(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _require_keys(value: Any, expected: set[str]) -> None:
    if not isinstance(value, dict) or set(value) != expected:
        raise ValueError


def _validate_model(profile: Any) -> None:
    """Reject malformed/publicly unsafe input without consulting local files."""
    try:
        _require_keys(profile, {"schema_version", "input", "privacy", "row_summary", "columns"})
        if profile["schema_version"] != "cleaning-profile/model/1":
            raise ValueError
        metadata = profile["input"]
        _require_keys(metadata, {"sha256", "columns", "row_count"})
        validate_recipe({
            "schema_version": "cleaning-recipe/1",
            "input": {"sha256": metadata["sha256"], "columns": metadata["columns"]},
            "steps": [],
        })
        if not _integer(metadata["row_count"]):
            raise ValueError
        if any(_unsafe_value(name) for name in metadata["columns"]):
            raise ValueError
        _require_keys(profile["privacy"], {"k"})
        k = profile["privacy"]["k"]
        if not _integer(k) or k < 5:
            raise ValueError
        summary = profile["row_summary"]
        _require_keys(summary, {"duplicate_row_count", "duplicate_group_count"})
        if any(not _integer(summary[key]) for key in ("duplicate_row_count", "duplicate_group_count")):
            raise ValueError
        columns = profile["columns"]
        if not isinstance(columns, list) or [column["name"] for column in columns] != metadata["columns"]:
            raise ValueError
        for column in columns:
            kind = column["kind"]
            if kind not in _KINDS:
                raise ValueError
            value_key = {"code_like": "values", "numeric_with_tokens": "non_numeric_values"}.get(kind)
            column_keys = {"name", "kind", "counts", "numeric_ratio", "patterns", "other_values"}
            if value_key:
                column_keys.add(value_key)
            _require_keys(column, column_keys)
            _require_keys(column["counts"], set(_COUNT_FIELDS))
            if any(not _integer(column["counts"][key]) for key in _COUNT_FIELDS):
                raise ValueError
            ratio = column["numeric_ratio"]
            if isinstance(ratio, bool) or not isinstance(ratio, (float, int)) or not math.isfinite(ratio) or not 0 <= ratio <= 1:
                raise ValueError
            patterns = column["patterns"]
            if not isinstance(patterns, list) or len(patterns) > 10:
                raise ValueError
            for item in patterns:
                _require_keys(item, {"pattern", "count"})
                if not isinstance(item["pattern"], str) or not _integer(item["count"]) or item["count"] == 0:
                    raise ValueError
                if _unsafe_value(item["pattern"]):
                    raise ValueError
            _require_keys(column["other_values"], {"distinct", "count"})
            if any(not _integer(column["other_values"][key]) for key in ("distinct", "count")):
                raise ValueError
            if value_key:
                values = column[value_key]
                if not isinstance(values, list):
                    raise ValueError
                seen = set()
                for item in values:
                    _require_keys(item, {"value", "count"})
                    value = item["value"]
                    if (
                        not isinstance(value, str) or not value or value in seen
                        or not _integer(item["count"]) or item["count"] < k
                        or _unsafe_value(value)
                    ):
                        raise ValueError
                    seen.add(value)
    except (KeyError, TypeError, ValueError, RecipeError, OverflowError):
        raise IssueError("profile_invalid", "问题建议需要结构完整且符合隐私规则的 model_profile。") from None


def _canonical_code(value: str) -> str:
    return value.translate(_FULLWIDTH).strip().translate(_ASCII_LOWER)


def _has_variants(values: list[str]) -> bool:
    canonical = [_canonical_code(value) for value in values]
    if len(set(canonical)) < len(canonical):
        return True
    return any(len(set(canonical) & group) > 1 for group in _ALIAS_GROUPS)


def propose_from_profile(model_profile: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use only this in-memory public profile; all returned steps stay pending."""
    _validate_model(model_profile)
    source = model_profile
    cards: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    input_columns = source["input"]["columns"]
    used_columns = set(input_columns)

    def evidence(*pointers: str) -> list[dict[str, Any]]:
        result = []
        for pointer in pointers:
            value: Any = source
            for token in pointer.lstrip("/").split("/"):
                token = token.replace("~1", "/").replace("~0", "~")
                value = value[int(token)] if isinstance(value, list) else value[token]
            result.append({"pointer": pointer, "value": copy.deepcopy(value)})
        return result

    def output_column(base: str) -> str:
        candidate, suffix = base, 2
        while candidate in used_columns:
            candidate = f"{base}_{suffix}"
            suffix += 1
        used_columns.add(candidate)
        return candidate

    def add(
        column: str | None, category: str, title: str, affected: int,
        proof: list[dict[str, Any]], needs: list[str],
        op: str | None = None, params: dict[str, Any] | None = None,
    ) -> None:
        step_ids = []
        if op is not None:
            step_id = f"S{len(steps) + 1}"
            step_ids.append(step_id)
            steps.append({
                "step_id": step_id, "op": op, "params": params,
                "reason": title, "approval": {"status": "pending"},
            })
        cards.append({
            "issue_id": f"I{len(cards) + 1}", "column": column,
            "category": category, "title": title, "affected_rows": affected,
            "evidence": proof, "suggested_step_ids": step_ids,
            "needs_human": needs,
        })

    # Check original complete rows before any normalizations change their keys.
    duplicate_rows = source["row_summary"]["duplicate_row_count"]
    if duplicate_rows:
        add(
            None, "合理性", "发现完全重复的原始行", duplicate_rows,
            evidence("/row_summary/duplicate_row_count", "/row_summary/duplicate_group_count", "/input/columns"),
            ["确认完全重复行是否属于重复记录；此步骤只标记，不删除行。"],
            "flag_duplicates", {"key_columns": list(input_columns)},
        )

    for index, column in enumerate(source["columns"]):
        base = f"/columns/{index}"
        name, kind, counts = column["name"], column["kind"], column["counts"]
        if kind == "identifier_like":
            add(
                name, "合理性", "标识符列需要去标识化", counts["non_empty"],
                evidence(f"{base}/kind", f"{base}/counts/non_empty", f"{base}/counts/leading_zero"),
                ["将在去标识化阶段处理。"],
            )
            continue
        visible_key = {"code_like": "values", "numeric_with_tokens": "non_numeric_values"}.get(kind)
        visible = column[visible_key] if visible_key else []
        if counts["normalization_affected"]:
            add(
                name, "符合性", "存在全角字符或首尾空白", counts["normalization_affected"],
                evidence(f"{base}/counts/normalization_affected", f"{base}/counts/fullwidth_characters", f"{base}/counts/outer_whitespace_values"),
                ["确认允许将全角 ASCII 字符转为半角并去除首尾空白。"],
                "normalize_text", {"column": name},
            )
        if counts["sentinel_candidate"]:
            candidates = sorted(item["value"] for item in visible if item["value"] in SENTINEL_VALUES)
            params: dict[str, Any] = {"column": name}
            needs = ["候选代码值需要逐个确认；确认表示缺失后才批准转换。"]
            pointers = [f"{base}/counts/sentinel_candidate"]
            if candidates:
                params["values"] = candidates
                pointers.append(f"{base}/{visible_key}")
            else:
                needs.append("公开剖析未提供具体候选值，请在本机核对并填写 values 后批准。")
            public_count = sum(item["count"] for item in visible if item["value"] in SENTINEL_VALUES)
            if public_count < counts["sentinel_candidate"]:
                needs.append("聚合计数包含未公开的候选取值；需在本机补充并逐个确认，不能由格式模式猜测。")
            add(
                name, "完整性", "存在可能表示缺失的代码值", counts["sentinel_candidate"],
                evidence(*pointers), needs, "sentinel_to_missing", params,
            )
        formats: list[str] = []
        for item in column["patterns"]:
            for date_format in DATE_FORMATS_BY_PATTERN.get(item["pattern"], []):
                if date_format not in formats:
                    formats.append(date_format)
        excel = counts["excel_serial_candidate"]
        if len(formats) > 1 or excel:
            params = {
                "column": name, "output_column": output_column(f"{name}_parsed"),
                "output": "datetime" if any("%H" in fmt for fmt in formats) else "date",
                "excel_serial": {"enabled": bool(excel), "min": 20000, "max": 60000},
            }
            needs = ["确认日期格式、输出精度及 Excel 序列日期候选确实代表日期。"]
            if formats:
                params["formats"] = formats
            else:
                needs.append("公开的前十种模式不足以推导日期格式，请在本机填写 formats 后批准。")
            add(
                name, "符合性", "日期存在多种格式或 Excel 序列候选",
                min(counts["non_empty"], counts["date_parseable"] + excel),
                evidence(f"{base}/patterns", f"{base}/counts/date_parseable", f"{base}/counts/excel_serial_candidate"),
                needs, "parse_date", params,
            )
        if kind == "age_text_like":
            add(
                name, "符合性", "年龄文本需要拆分为年月天", counts["age_text"],
                evidence(f"{base}/kind", f"{base}/counts/age_text", f"{base}/patterns"),
                ["确认将年龄拆分为年、月、天，未写明的分量按原操作规则填 0。"],
                "parse_age_cn", {
                    "column": name,
                    "outputs": {key: output_column(f"{name}_{key}") for key in ("years", "months", "days")},
                },
            )
        if kind == "numeric_with_tokens":
            needs = ["确认定性词表和无效词表适用于本列；未识别的值会保留并标记。"]
            for item in visible:
                value = item["value"]
                if value not in _QUALITATIVE_VALUES + _INVALID_VALUES and not ops.is_number(value) and not _QUALIFIED_NUMBER.fullmatch(value):
                    needs.append(f"公开取值“{value}”尚未归类，请决定是否补入定性词表或无效词表。")
            pointers = [f"{base}/kind", f"{base}/counts/non_empty", f"{base}/non_numeric_values"]
            if column["other_values"]["count"]:
                pointers.append(f"{base}/other_values")
                needs.append("另有未公开的非数字取值，仅提供聚合计数；请在本机核对后决定如何处理。")
            add(
                name, "符合性", "结果中混有数值、限定符或文本", counts["non_empty"],
                evidence(*pointers), needs, "parse_lab_result", {
                    "column": name, "qualitative_values": list(_QUALITATIVE_VALUES),
                    "invalid_values": list(_INVALID_VALUES),
                    "outputs": {key: output_column(f"{name}_{key}") for key in ("value", "qualifier", "qualitative", "invalid")},
                },
            )
        if kind == "code_like" and _has_variants([item["value"] for item in visible]):
            needs = [
                "公开取值存在同形或已知别名候选，需要人确认是否同义并填写映射；不能自动认定等价。",
                "已经是规范写法的值也要填（例如 男→男），否则执行时会被标为未映射。",
                "映射目标不能为空；表示缺失的值应使用 sentinel_to_missing。",
            ]
            if counts["normalization_affected"]:
                needs.append("前面的 normalize_text 会改变部分键；请按实际执行顺序补齐规范化后的键。")
            if column["other_values"]["count"]:
                needs.append("低频或敏感取值未公开，无法列入 mapping；请在本机核对并补充。")
            add(
                name, "符合性", "代码值存在可能同义的多种写法", sum(item["count"] for item in visible),
                evidence(f"{base}/values", f"{base}/other_values"), needs,
                "map_values", {
                    "column": name, "mapping": {item["value"]: "" for item in visible},
                    "on_unmapped": "keep_and_flag",
                },
            )
    recipe = {
        "schema_version": "cleaning-recipe/1",
        "input": {"sha256": source["input"]["sha256"], "columns": list(input_columns)},
        "steps": steps,
    }
    validate_recipe(recipe)
    return {"schema_version": "cleaning-issues/1", "issues": cards}, recipe
