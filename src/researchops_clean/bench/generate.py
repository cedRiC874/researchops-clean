"""Generate deterministic, hand-designed synthetic pediatric laboratory tables."""
from __future__ import annotations

import calendar
import math
import random
from collections import Counter
from datetime import date, timedelta
from decimal import Decimal

from .. import csv_io, ops
from ..engine import _sha256
from ..ledger import sha256_json
from .common import BenchmarkError, GENERATOR_VERSION, json_bytes, publish_files
from .reference import COLUMNS, PROJECT_CONFIG, build_reference_recipe, fixed_value


ERROR_TYPES = (
    "date_format", "sex_code", "item_alias", "unit_variant", "fullwidth_space",
    "sentinel", "impossible_value", "duplicate_record",
)
DEFAULT_RATES = {name: 0.03 for name in ERROR_TYPES}
DEFAULT_RATES["duplicate_record"] = 0.02
_FULLWIDTH = str.maketrans(
    {character: chr(ord(character) + 0xFEE0) for character in "0123456789."}
)
_GENERATED_README = """# 合成评测数据

本目录全部为程序生成的工程样例，不含真实患者数据。

**参考配方仅用于评测。真实数据必须由人逐步审核并批准，不能直接采用这里的批准记录。**

reference_recipe.json 的批准人是 bench-reference，时间为固定值；每步理由以
“【仅用于评测】”开头。范围检查中的界限仅用于识别预设注入错误，不是医学参考区间。

标准答案的数值始终保留一位小数。公开的工程数值网格为：血红蛋白 80.0–160.0、
白细胞计数 2.0–15.0、超敏C反应蛋白 0.0–20.0、血糖 2.5–9.5，各以 0.1 为步长。
这些范围仅控制合成分布，不用于临床判定。

参考配方复用数值解析和单位换算操作，再用完整公开网格的映射恢复一位小数。
血糖换算使用固定高精度十进制近似，网格映射同时覆盖其换算结果。
该映射由公开配置全域生成，不从本次 clean.csv 或 truth.json 提取答案。

truth.json 使用从 1 开始的行号。重复组的原行和副本都记录 duplicate_record；
副本如带写法变化，相应单元格改记该写法错误，每个单元格只出现一种错误。
bench_manifest.json 中 error_counts 统计错误单元格，event_counts 另统计注入事件；
重复事件是一组原行与副本，单位变化事件同时影响结果和单位。

使用 reference_column_map.json 对齐参考配方产生的日期列和数值列。
README.md 本身也由 bench_manifest.json 的文件哈希保护。
"""


def _age_text(birth: date, sampled: date) -> str:
    months = (sampled.year - birth.year) * 12 + sampled.month - birth.month
    if sampled.day < birth.day:
        months -= 1
    year, month = divmod(birth.year * 12 + birth.month - 1 + months, 12)
    anchor = date(year, month + 1, min(birth.day, calendar.monthrange(year, month + 1)[1]))
    years, remaining_months = divmod(months, 12)
    days = (sampled - anchor).days
    return (
        (f"{years}岁" if years else "")
        + (f"{remaining_months}月" if remaining_months else "")
        + (f"{days}天" if days else "")
    ) or "0天"


def _parameters(rows, seed, rates):
    if type(rows) is not int or not 1 <= rows <= csv_io.MAX_DATA_ROWS // 2:
        raise BenchmarkError("benchmark_parameters_invalid", "rows 必须是允许范围内的正整数。")
    if type(seed) is not int:
        raise BenchmarkError("benchmark_parameters_invalid", "seed 必须是整数。")
    if rates is not None and not isinstance(rates, dict):
        raise BenchmarkError("benchmark_parameters_invalid", "错误比例必须按类型提供。")
    actual = dict(DEFAULT_RATES)
    for name, value in (rates or {}).items():
        if (
            name not in ERROR_TYPES or isinstance(value, bool)
            or not isinstance(value, (int, float)) or not math.isfinite(value)
            or not 0 <= value <= 1
        ):
            raise BenchmarkError("benchmark_parameters_invalid", "错误类型或比例不合法，比例须在 0 至 1 之间。")
        actual[name] = float(value)
    return actual


def _clean_rows(rows: int, rng: random.Random) -> list[dict[str, str]]:
    result = []
    identifiers = set()
    projects = list(PROJECT_CONFIG)
    birth_start = date(2010, 1, 1)
    birth_days = (date(2023, 12, 31) - birth_start).days
    while len(result) < rows:
        identifier = f"0{rng.randrange(1_000_000):06d}"
        if identifier in identifiers:
            continue
        identifiers.add(identifier)
        birth = birth_start + timedelta(days=rng.randrange(birth_days + 1))
        sex = rng.choice(("男", "女"))
        base_sample = date(2024, 1, 1) + timedelta(days=rng.randrange(180))
        for offset, item in enumerate(projects):
            if len(result) == rows:
                break
            sampled = base_sample + timedelta(days=offset)
            config = PROJECT_CONFIG[item]
            value = rng.randint(config["min_tenths"], config["max_tenths"])
            result.append({
                "住院号": identifier, "出生日期": birth.isoformat(), "性别": sex,
                "采样时间": sampled.isoformat(), "项目": item, "结果": fixed_value(value),
                "单位": config["unit"], "年龄": _age_text(birth, sampled),
            })
    return result


def _date_variant(value: str, rng: random.Random) -> str:
    sampled = date.fromisoformat(value)
    choices = (
        f"{sampled.year}/{sampled.month}/{sampled.day} 8:30",
        sampled.strftime("%Y%m%d"), str((sampled - date(1899, 12, 30)).days),
    )
    return rng.choice(choices)


def _sex_variant(value: str, rng: random.Random) -> str:
    return rng.choice(("1", "M") if value == "男" else ("2", "F"))


def _text_variant(value: str, rng: random.Random) -> str:
    return value.translate(_FULLWIDTH) if rng.randrange(2) else f" {value} "


def _error_count(population: int, rate: float) -> int:
    return int(population * rate + 0.5)


def _inject(clean: list[dict[str, str]], rng: random.Random, rates: dict) -> tuple[list, dict, dict]:
    entries = [
        {"clean_row": index, "row": dict(row), "is_duplicate": False,
         "duplicate_group": None, "errors": {}}
        for index, row in enumerate(clean, 1)
    ]
    events = {name: 0 for name in ERROR_TYPES}

    def mark(entry, column, error_type, expected):
        entry["errors"][column] = (error_type, expected)

    duplicate_indices = rng.sample(
        range(len(clean)), _error_count(len(clean), rates["duplicate_record"]),
    )
    for group, index in enumerate(sorted(duplicate_indices), 1):
        original = entries[index]
        original["duplicate_group"] = group
        duplicate = {
            "clean_row": original["clean_row"], "row": dict(original["row"]),
            "is_duplicate": True, "duplicate_group": group, "errors": {},
        }
        for column in COLUMNS:
            mark(original, column, "duplicate_record", "flag_only")
            mark(duplicate, column, "duplicate_record", "flag_only")
        variations = [
            name for name in ("date_format", "sex_code", "fullwidth_space") if rates[name] > 0
        ]
        if variations:
            variation = rng.choice(variations)
            column, transform = {
                "date_format": ("采样时间", _date_variant),
                "sex_code": ("性别", _sex_variant),
                "fullwidth_space": ("结果", _text_variant),
            }[variation]
            duplicate["row"][column] = transform(duplicate["row"][column], rng)
            mark(duplicate, column, variation, "restore")
            events[variation] += 1
        original["copy"] = duplicate
        events["duplicate_record"] += 1
    copies = [entry.pop("copy") for entry in entries if "copy" in entry]
    for duplicate in copies:
        entries.insert(rng.randrange(len(entries) + 1), duplicate)

    def choose(error_type, columns, eligible=lambda row: True, population=None):
        available = [
            entry for entry in entries
            if entry["duplicate_group"] is None and eligible(entry["row"])
            and all(column not in entry["errors"] for column in columns)
        ]
        if population is None:
            population = sum(eligible(row) for row in clean)
        count = min(len(available), _error_count(population, rates[error_type]))
        return rng.sample(available, count)

    for entry in choose("date_format", ["采样时间"]):
        entry["row"]["采样时间"] = _date_variant(entry["row"]["采样时间"], rng)
        mark(entry, "采样时间", "date_format", "restore")
        events["date_format"] += 1
    for entry in choose("sex_code", ["性别"]):
        entry["row"]["性别"] = _sex_variant(entry["row"]["性别"], rng)
        mark(entry, "性别", "sex_code", "restore")
        events["sex_code"] += 1
    for entry in choose("item_alias", ["项目"], lambda row: bool(PROJECT_CONFIG[row["项目"]]["aliases"])):
        entry["row"]["项目"] = rng.choice(PROJECT_CONFIG[entry["row"]["项目"]]["aliases"])
        mark(entry, "项目", "item_alias", "restore")
        events["item_alias"] += 1

    # Unit and impossible-value eligibility uses the immutable canonical item;
    # an independently injected alias must not change which rows are eligible.
    def item_of(entry):
        return clean[entry["clean_row"] - 1]["项目"]

    unit_population = sum(row["项目"] in {"血红蛋白", "血糖"} for row in clean)
    unit_available = [
        entry for entry in entries if entry["duplicate_group"] is None
        and item_of(entry) in {"血红蛋白", "血糖"}
        and all(column not in entry["errors"] for column in ("结果", "单位"))
    ]
    for entry in rng.sample(
        unit_available, min(len(unit_available), _error_count(unit_population, rates["unit_variant"])),
    ):
        glucose = item_of(entry) == "血糖"
        value = Decimal(entry["row"]["结果"]) * (18 if glucose else Decimal("0.1"))
        entry["row"]["结果"] = ops.decimal_text(value)
        entry["row"]["单位"] = "mg/dL" if glucose else "g/dL"
        for column in ("结果", "单位"):
            mark(entry, column, "unit_variant", "restore")
        events["unit_variant"] += 1
    for entry in choose("fullwidth_space", ["结果"]):
        entry["row"]["结果"] = _text_variant(entry["row"]["结果"], rng)
        mark(entry, "结果", "fullwidth_space", "restore")
        events["fullwidth_space"] += 1
    sentinel_candidates = [
        (entry, column) for entry in entries if entry["duplicate_group"] is None
        for column in ("年龄", "采样时间") if column not in entry["errors"]
    ]
    sentinel_count = min(
        len(sentinel_candidates), _error_count(len(clean) * 2, rates["sentinel"]),
    )
    for entry, column in rng.sample(sentinel_candidates, sentinel_count):
        entry["row"][column] = "999" if column == "年龄" else "1900-01-01"
        mark(entry, column, "sentinel", "to_missing")
        events["sentinel"] += 1
    impossible_available = [
        entry for entry in entries if entry["duplicate_group"] is None
        and item_of(entry) == "血红蛋白" and "结果" not in entry["errors"]
    ]
    impossible_count = min(
        len(impossible_available),
        _error_count(sum(row["项目"] == "血红蛋白" for row in clean), rates["impossible_value"]),
    )
    for entry in rng.sample(impossible_available, impossible_count):
        entry["row"]["结果"] = format(Decimal(entry["row"]["结果"]) * 10, ".1f")
        mark(entry, "结果", "impossible_value", "flag_only")
        events["impossible_value"] += 1

    errors, row_mapping, dirty = [], [], []
    for row_number, entry in enumerate(entries, 1):
        dirty.append(entry["row"])
        row_mapping.append({
            "dirty_row": row_number, "clean_row": entry["clean_row"],
            "is_duplicate": entry["is_duplicate"], "duplicate_group": entry["duplicate_group"],
        })
        for column in COLUMNS:
            if column in entry["errors"]:
                error_type, expected = entry["errors"][column]
                errors.append({
                    "row": row_number, "column": column,
                    "clean_value": clean[entry["clean_row"] - 1][column],
                    "dirty_value": entry["row"][column], "error_type": error_type,
                    "expected": expected,
                })
    truth = {
        "schema_version": "benchmark-truth/1", "columns": list(COLUMNS),
        "row_mapping": row_mapping, "errors": errors,
    }
    return dirty, truth, events


def generate_benchmark(rows=2000, seed=7, *, output_dir, project_root=None, rates=None) -> dict:
    actual_rates = _parameters(rows, seed, rates)
    rng = random.Random(seed)
    clean = _clean_rows(rows, rng)
    dirty, truth, events = _inject(clean, rng, actual_rates)
    clean_bytes = csv_io.csv_bytes(COLUMNS, clean)
    dirty_bytes = csv_io.csv_bytes(COLUMNS, dirty)
    if max(len(clean_bytes), len(dirty_bytes)) > csv_io.MAX_INPUT_BYTES:
        raise BenchmarkError("benchmark_too_large", "生成 CSV 超过现有读取器的大小限制。")
    recipe, column_map = build_reference_recipe(_sha256(dirty_bytes))
    files = {
        "clean.csv": clean_bytes, "dirty.csv": dirty_bytes,
        "truth.json": json_bytes(truth), "reference_recipe.json": json_bytes(recipe),
        "reference_column_map.json": json_bytes(column_map),
        "README.md": _GENERATED_README.encode("utf-8"),
    }
    counts = Counter(error["error_type"] for error in truth["errors"])
    manifest = {
        "schema_version": "cleaning-benchmark/1", "generator_version": GENERATOR_VERSION,
        "parameters": {"rows": rows, "seed": seed, "rates": actual_rates}, "seed": seed,
        "files": {filename: _sha256(data) for filename, data in files.items()},
        "error_counts": {name: counts[name] for name in ERROR_TYPES},
        "event_counts": events,
    }
    manifest["bench_hash"] = sha256_json(manifest)
    files["bench_manifest.json"] = json_bytes(manifest)
    publish_files(output_dir, files, project_root=project_root)
    return manifest
