"""A public synthetic value grid and an explicitly evaluation-only recipe."""
from __future__ import annotations

from decimal import Decimal

from .. import ops
from ..recipe import approve_recipe, validate_steps
from .common import FIXED_TIMESTAMP


COLUMNS = ["住院号", "出生日期", "性别", "采样时间", "项目", "结果", "单位", "年龄"]

# These finite engineering grids generate examples, not clinical reference ranges.
PROJECT_CONFIG = {
    "血红蛋白": {"unit": "g/L", "min_tenths": 800, "max_tenths": 1600, "aliases": ["HGB", "Hb"]},
    "白细胞计数": {"unit": "×10^9/L", "min_tenths": 20, "max_tenths": 150, "aliases": ["WBC"]},
    "超敏C反应蛋白": {"unit": "mg/L", "min_tenths": 0, "max_tenths": 200, "aliases": []},
    "血糖": {"unit": "mmol/L", "min_tenths": 25, "max_tenths": 95, "aliases": ["GLU"]},
}
GLUCOSE_FACTOR = "0.0555555555555555555555555555555555555555555555555555555555555556"
REFERENCE_COLUMN_MAP = {"采样时间": "采样日期", "结果": "结果数值"}


def fixed_value(tenths: int) -> str:
    return format(Decimal(tenths) / 10, ".1f")


def _unit_params(item: str) -> dict:
    glucose = item == "血糖"
    return {
        "value_column": "结果数值", "unit_column": "单位",
        "conversions": [{
            "from": "mg/dL" if glucose else "g/dL",
            "to": "mmol/L" if glucose else "g/L",
            "factor": GLUCOSE_FACTOR if glucose else "10",
        }],
        "where": {"column": "项目", "equals": item},
    }


def _precision_mapping() -> dict[str, str]:
    """Cover the complete configured grids, independent of any generated rows."""
    mapping = {}
    for config in PROJECT_CONFIG.values():
        for tenths in range(config["min_tenths"], config["max_tenths"] + 1):
            formatted = fixed_value(tenths)
            mapping[formatted] = formatted
            mapping[ops.decimal_text(Decimal(formatted))] = formatted
    config = PROJECT_CONFIG["血糖"]
    tenths_grid = range(config["min_tenths"], config["max_tenths"] + 1)
    synthetic = [
        {"项目": "血糖", "单位": "mg/dL",
         "结果数值": ops.decimal_text(Decimal(tenths) * Decimal("1.8"))}
        for tenths in tenths_grid
    ]
    # Reuse the executor's exact precision policy instead of reimplementing it.
    ops.execute_operation(
        "convert_unit", _unit_params("血糖"), ["项目", "单位", "结果数值"], synthetic, "grid",
    )
    for tenths, row in zip(tenths_grid, synthetic):
        mapping[row["结果数值"]] = fixed_value(tenths)
    return mapping


def build_reference_recipe(input_sha256: str) -> tuple[dict, dict]:
    """Build from public configuration only; no clean table or truth is accepted."""
    steps = []

    def add(op: str, params: dict, reason: str) -> None:
        steps.append({
            "step_id": f"B{len(steps) + 1}", "op": op, "params": params,
            "reason": "【仅用于评测】" + reason,
            "approval": {"status": "pending"},
        })

    add("normalize_text", {"column": "结果"}, "恢复合成结果的全角字符和首尾空白。")
    add("map_values", {
        "column": "性别", "mapping": {"男": "男", "女": "女", "1": "男", "2": "女", "M": "男", "F": "女"},
        "on_unmapped": "keep_and_flag",
    }, "按生成器公开编码表恢复性别。")
    item_mapping = {item: item for item in PROJECT_CONFIG}
    for item, config in PROJECT_CONFIG.items():
        item_mapping.update({alias: item for alias in config["aliases"]})
    add("map_values", {
        "column": "项目", "mapping": item_mapping, "on_unmapped": "keep_and_flag",
    }, "按生成器公开别名表恢复项目名称。")
    add("sentinel_to_missing", {"column": "年龄", "values": ["999"]},
        "将已知注入的年龄代码置空并标记。")
    add("sentinel_to_missing", {"column": "采样时间", "values": ["1900-01-01"]},
        "将已知注入的采样日期代码置空并标记。")
    add("parse_date", {
        "column": "采样时间", "output_column": "采样日期",
        "formats": ["%Y-%m-%d", "%Y/%m/%d %H:%M", "%Y%m%d"], "output": "date",
        "excel_serial": {"enabled": True, "min": 20000, "max": 60000},
    }, "解析公开约定的日期格式；输出仍为日期。")
    add("parse_lab_result", {
        "column": "结果", "qualitative_values": [], "invalid_values": [],
        "outputs": {"value": "结果数值", "qualifier": "结果限定符",
                    "qualitative": "结果定性", "invalid": "结果无效"},
    }, "以原有解析操作提取合成数值。")
    for item in ("血红蛋白", "血糖"):
        add("convert_unit", _unit_params(item), f"恢复合成{item}单位。")
    add("map_values", {
        "column": "结果数值", "mapping": _precision_mapping(), "on_unmapped": "keep",
    }, "按完整公开数值网格恢复一位小数；字典不读取本次答案。")
    add("range_check", {
        "column": "结果数值", "min": "0", "max": "300",
        "where": {"column": "项目", "equals": "血红蛋白"},
    }, "以工程示例界限标出多录一个零的值；此界限不是医学参考区间。")
    add("flag_duplicates", {
        "key_columns": [REFERENCE_COLUMN_MAP.get(column, column) for column in COLUMNS],
    }, "按全部标准化列标记复制的原行和副本，不删除任何行。")
    recipe = {
        "schema_version": "cleaning-recipe/1",
        "input": {"sha256": input_sha256, "columns": list(COLUMNS)}, "steps": steps,
    }
    recipe = approve_recipe(
        recipe, [step["step_id"] for step in steps], "bench-reference", FIXED_TIMESTAMP,
    )
    validate_steps(recipe)
    return recipe, dict(REFERENCE_COLUMN_MAP)
