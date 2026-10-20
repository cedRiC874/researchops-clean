"""Cleaning recipe structure, approval decisions, and canonical hashes."""

from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import ops
from .ledger import sha256_json


class RecipeError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


_REVIEWER_SCHEMA = {
    "type": "string",
    "minLength": 1,
    "maxLength": 40,
    "pattern": r"^[^/\\\x00-\x1f\x7f-\x9f]+$",
}
_COLUMN_SCHEMA = {"type": "string", "minLength": 1, "pattern": r"\S"}
RECIPE_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "input", "steps"],
    "properties": {
        "schema_version": {"const": "cleaning-recipe/1"},
        "input": {
            "type": "object",
            "additionalProperties": False,
            "required": ["sha256", "columns"],
            "properties": {
                "sha256": {
                    "type": "string",
                    "minLength": 64,
                    "maxLength": 64,
                    "pattern": "^[0-9a-f]{64}$",
                },
                "columns": {
                    "type": "array",
                    "minItems": 1,
                    "uniqueItems": True,
                    "items": _COLUMN_SCHEMA,
                },
            },
        },
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["step_id", "op", "params", "reason", "approval"],
                "properties": {
                    "step_id": {"type": "string", "minLength": 1},
                    "op": {"type": "string", "minLength": 1},
                    "params": {"type": "object"},
                    "reason": {"type": "string"},
                    "approval": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["status"],
                        "properties": {
                            "status": {"enum": ["pending", "approved", "rejected"]},
                            "reviewer": _REVIEWER_SCHEMA,
                            "decided_at_utc": {"type": "string"},
                            "content_sha256": {"type": "string"},
                        },
                        "if": {
                            "properties": {"status": {"const": "approved"}},
                            "required": ["status"],
                        },
                        "then": {"required": ["reviewer", "decided_at_utc"]},
                    },
                },
            },
        },
    },
}
_VALIDATOR = Draft202012Validator(RECIPE_SCHEMA)
_REVIEWER_VALIDATOR = Draft202012Validator(_REVIEWER_SCHEMA)
_UTC_TIMESTAMP = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z")


def _validate_timestamp(value: str) -> None:
    if not isinstance(value, str) or not _UTC_TIMESTAMP.fullmatch(value):
        raise RecipeError("recipe_invalid", "批准时间必须是以 Z 结尾的 UTC 时间。")
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise RecipeError("recipe_invalid", "批准时间不是有效的 UTC 时间。") from None


def _reject_constant(value: str) -> None:
    raise ValueError("JSON 数值必须有限。")


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("JSON 字段不能重复。")
        result[key] = value
    return result


def load_recipe(path: Path) -> dict[str, Any]:
    try:
        raw = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        raise RecipeError("recipe_read_error", "无法读取 UTF-8 配方文件。") from None
    try:
        recipe = json.loads(
            raw, parse_constant=_reject_constant, object_pairs_hook=_unique_object
        )
    except (ValueError, RecursionError):
        raise RecipeError("recipe_invalid", "配方必须是字段无重复的有效 JSON。") from None
    validate_recipe(recipe)
    return recipe


def validate_recipe(recipe: dict[str, Any]) -> None:
    """Validate structure only; operation parameters may remain incomplete."""

    try:
        json.dumps(recipe, allow_nan=False)
        invalid = next(_VALIDATOR.iter_errors(recipe), None)
    except (TypeError, ValueError, RecursionError):
        raise RecipeError("recipe_invalid", "配方必须只包含有效的 JSON 值。") from None
    if invalid is not None:
        raise RecipeError("recipe_invalid", "配方结构或字段类型不符合 cleaning-recipe/1。")
    step_ids: set[str] = set()
    for step in recipe["steps"]:
        if step["step_id"] in step_ids:
            raise RecipeError("recipe_invalid", "配方中的 step_id 必须唯一。")
        step_ids.add(step["step_id"])
        approval = step["approval"]
        if "decided_at_utc" in approval:
            _validate_timestamp(approval["decided_at_utc"])


def _validate_operation(step: dict[str, Any], columns: list[str]) -> list[str]:
    try:
        return ops.validate_operation(step["op"], step["params"], columns)
    except ops.OperationError as exc:
        raise RecipeError(exc.code, exc.message) from None


def step_content_hash(recipe: dict[str, Any], step: dict[str, Any]) -> str:
    """Bind exact Unicode input and executable content, excluding reason."""
    content = {
        "input_sha256": recipe["input"]["sha256"],
        "step_id": step["step_id"],
        "op": step["op"],
        "params": step["params"],
    }
    # The ledger normalizes NFC; execution distinguishes literal column names
    # and mapping strings, so approval must preserve their original code points.
    encoded = json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_approval_content(recipe: dict[str, Any]) -> None:
    """Check approved steps in a structurally validated recipe."""
    mismatched = [
        step["step_id"] for step in recipe["steps"]
        if step["approval"]["status"] == "approved"
        and step["approval"].get("content_sha256") != step_content_hash(recipe, step)
    ]
    if mismatched:
        raise RecipeError(
            "approval_content_mismatch",
            "以下步骤的批准内容哈希缺失或不匹配，必须重新批准：" + "、".join(mismatched) + "。",
        )


def validate_steps(
    recipe: dict[str, Any], require_approved: bool = True
) -> None:
    validate_recipe(recipe)
    if require_approved:
        unapproved = [
            step["step_id"]
            for step in recipe["steps"]
            if step["approval"]["status"] != "approved"
        ]
        if unapproved:
            raise RecipeError(
                "recipe_step_not_approved", "以下步骤尚未批准：" + "、".join(unapproved) + "。"
            )
    validate_approval_content(recipe)
    columns = list(recipe["input"]["columns"])
    for step in recipe["steps"]:
        columns.extend(_validate_operation(step, columns))


def recipe_hash(recipe: dict[str, Any]) -> str:
    return sha256_json(recipe)


def approve_recipe(
    recipe: dict[str, Any],
    step_ids: list[str],
    reviewer: str,
    decided_at_utc: str | None = None,
) -> dict[str, Any]:
    """Return a copy with only the selected steps validated and approved."""

    validate_recipe(recipe)
    if not _REVIEWER_VALIDATOR.is_valid(reviewer):
        raise RecipeError("reviewer_invalid", "reviewer 须为 1 至 40 个字符，且不含路径分隔符或控制字符。")
    if (
        not isinstance(step_ids, list)
        or not step_ids
        or any(not isinstance(step_id, str) or not step_id for step_id in step_ids)
        or len(set(step_ids)) != len(step_ids)
    ):
        raise RecipeError("recipe_invalid", "待批准的 step_id 必须是非空且不重复的列表。")
    selected = set(step_ids)
    available_ids = {step["step_id"] for step in recipe["steps"]}
    if not selected <= available_ids:
        raise RecipeError("recipe_step_missing", "待批准的 step_id 不在配方中。")
    timestamp = decided_at_utc
    if timestamp is None:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    _validate_timestamp(timestamp)

    approved = copy.deepcopy(recipe)
    columns = list(approved["input"]["columns"])
    for step in approved["steps"]:
        if step["step_id"] in selected:
            new_columns = _validate_operation(step, columns)
            step["approval"] = {
                "status": "approved",
                "reviewer": reviewer,
                "decided_at_utc": timestamp,
                "content_sha256": step_content_hash(approved, step),
            }
        else:
            # A draft may declare a future column before its params are complete.
            new_columns = ops.declared_output_columns(step["op"], step["params"])
        columns.extend(new_columns)
    return approved
