"""Evaluate aligned cells against synthetic truth without replaying the run."""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from pathlib import Path

from ..csv_io import CSVError, Table, read_csv
from ..engine import CHANGE_COLUMNS, FLAG_COLUMNS, OUTPUT_NAMES, EngineError, _load_manifest, _sha256
from ..ledger import AuditError, sha256_json
from ..recipe import RecipeError, _reject_constant, _unique_object, load_recipe, validate_steps
from .common import (
    BenchmarkError, FIXED_TIMESTAMP, json_bytes, project_path, publish_files, validate_target,
)
from .generate import ERROR_TYPES


BENCH_FILES = (
    "clean.csv", "dirty.csv", "truth.json", "reference_recipe.json",
    "reference_column_map.json", "README.md",
)
_EXPECTED = {
    "date_format": "restore", "sex_code": "restore", "item_alias": "restore",
    "unit_variant": "restore", "fullwidth_space": "restore",
    "sentinel": "to_missing", "impossible_value": "flag_only",
    "duplicate_record": "flag_only",
}
_LABELS = {
    "date_format": "日期格式", "sex_code": "性别编码", "item_alias": "项目别名",
    "unit_variant": "单位变化", "fullwidth_space": "全角或空白",
    "sentinel": "缺失代码", "impossible_value": "不可能值", "duplicate_record": "重复记录",
}


def _read_artifact(path: Path) -> bytes:
    try:
        if path.is_symlink() or not path.is_file():
            raise BenchmarkError("benchmark_file_missing", "评估所需文件缺失或不是普通文件。")
        return path.read_bytes()
    except OSError:
        raise BenchmarkError("benchmark_input_unreadable", "无法读取评估所需文件。") from None


def _json_file(path: Path) -> object:
    try:
        value = json.loads(
            _read_artifact(path).decode("utf-8"),
            object_pairs_hook=_unique_object, parse_constant=_reject_constant,
        )
        sha256_json(value)
        return value
    except (ValueError, UnicodeError, RecursionError, AuditError):
        raise BenchmarkError("benchmark_input_invalid", "评估输入必须是有效的 UTF-8 JSON。") from None


def _is_count(value: object) -> bool:
    return type(value) is int and value >= 0


def _load_benchmark(directory: Path) -> tuple[dict, Table, Table, dict, dict[int, int]]:
    manifest = _json_file(directory / "bench_manifest.json")
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != "cleaning-benchmark/1"
        or manifest.get("generator_version") != "1"
        or not isinstance(manifest.get("files"), dict)
        or set(manifest["files"]) != set(BENCH_FILES)
        or not isinstance(manifest.get("error_counts"), dict)
        or set(manifest["error_counts"]) != set(ERROR_TYPES)
        or any(not _is_count(value) for value in manifest["error_counts"].values())
    ):
        raise BenchmarkError("benchmark_manifest_invalid", "基准清单结构或版本不正确。")
    identity = {key: value for key, value in manifest.items() if key != "bench_hash"}
    if manifest.get("bench_hash") != sha256_json(identity):
        raise BenchmarkError("benchmark_hash_mismatch", "基准清单的哈希不一致。")
    for name in BENCH_FILES:
        digest = manifest["files"][name]
        if not isinstance(digest, str) or re.fullmatch("[0-9a-f]{64}", digest) is None:
            raise BenchmarkError("benchmark_manifest_invalid", "基准文件哈希格式不正确。")
        if _sha256(_read_artifact(directory / name)) != digest:
            raise BenchmarkError("benchmark_hash_mismatch", "基准文件已被改动或哈希不一致。")
    try:
        clean = read_csv(directory / "clean.csv", encoding="utf-8")
        dirty = read_csv(directory / "dirty.csv", encoding="utf-8")
        reference = load_recipe(directory / "reference_recipe.json")
        validate_steps(reference)
    except (CSVError, RecipeError) as exc:
        raise BenchmarkError(exc.code, exc.message) from None
    if clean.columns != dirty.columns:
        raise BenchmarkError("benchmark_columns_invalid", "干净表和脏表的列名或顺序不一致。")
    if reference["input"] != {"sha256": dirty.sha256, "columns": dirty.columns}:
        raise BenchmarkError("benchmark_reference_invalid", "参考配方未绑定本次脏表。")
    declared_reference_map = _json_file(directory / "reference_column_map.json")
    if (
        not isinstance(declared_reference_map, dict)
        or any(key not in clean.columns or not isinstance(value, str) or not value
               for key, value in declared_reference_map.items())
    ):
        raise BenchmarkError("column_map_invalid", "参考列映射格式不正确。")
    parameters = manifest.get("parameters")
    if not isinstance(parameters, dict) or parameters.get("rows") != len(clean.rows):
        raise BenchmarkError("benchmark_manifest_invalid", "基准清单的干净表行数不一致。")
    truth = _json_file(directory / "truth.json")
    aligned = _validate_truth(truth, clean, dirty, manifest)
    return manifest, clean, dirty, truth, aligned


def _validate_truth(truth: object, clean: Table, dirty: Table,
                    manifest: dict) -> dict[int, int]:
    if (
        not isinstance(truth, dict) or truth.get("schema_version") != "benchmark-truth/1"
        or truth.get("columns") != clean.columns
        or not isinstance(truth.get("row_mapping"), list)
        or not isinstance(truth.get("errors"), list)
        or len(truth["row_mapping"]) != len(dirty.rows)
    ):
        raise BenchmarkError("benchmark_truth_invalid", "基准真值或行对应关系不完整。")
    aligned = {}
    originals = Counter()
    groups = defaultdict(list)
    for item in truth["row_mapping"]:
        if not isinstance(item, dict):
            raise BenchmarkError("benchmark_truth_invalid", "行对应关系格式不正确。")
        row, clean_row = item.get("dirty_row"), item.get("clean_row")
        duplicate = item.get("is_duplicate")
        group = item.get("duplicate_group")
        if (
            type(row) is not int or not 1 <= row <= len(dirty.rows) or row in aligned
            or type(clean_row) is not int or not 1 <= clean_row <= len(clean.rows)
            or type(duplicate) is not bool
            or (group is not None and (type(group) is not int or group <= 0))
            or (duplicate and group is None)
        ):
            raise BenchmarkError("benchmark_truth_invalid", "行对应关系重复、越界或重复记录标识无效。")
        aligned[row] = clean_row
        if not duplicate:
            originals[clean_row] += 1
        if group is not None:
            groups[group].append((row, clean_row, duplicate))
    if set(originals) != set(range(1, len(clean.rows) + 1)) or any(value != 1 for value in originals.values()):
        raise BenchmarkError("benchmark_truth_invalid", "每条干净记录必须恰好对应一条未复制的脏记录。")
    for group in groups.values():
        if len({item[1] for item in group}) != 1 or sum(not item[2] for item in group) != 1 or len(group) < 2:
            raise BenchmarkError("benchmark_truth_invalid", "重复组必须包含同一干净记录的原行和副本。")
    injected = {}
    counts = Counter()
    for item in truth["errors"]:
        if not isinstance(item, dict):
            raise BenchmarkError("benchmark_truth_invalid", "错误单元格记录格式不正确。")
        row, column, error_type = item.get("row"), item.get("column"), item.get("error_type")
        if (
            type(row) is not int or row not in aligned
            or not isinstance(column, str) or column not in clean.columns
            or (row, column) in injected
            or error_type not in ERROR_TYPES
            or item.get("expected") != _EXPECTED[error_type]
            or item.get("clean_value") != clean.rows[aligned[row] - 1][column]
            or item.get("dirty_value") != dirty.rows[row - 1][column]
        ):
            raise BenchmarkError("benchmark_truth_invalid", "错误单元格存在重叠、越界、类型或取值不一致。")
        injected[(row, column)] = item
        counts[error_type] += 1
    if {name: counts[name] for name in ERROR_TYPES} != manifest["error_counts"]:
        raise BenchmarkError("benchmark_truth_invalid", "错误数量与基准清单不一致。")
    for row, clean_row in aligned.items():
        for column in clean.columns:
            if (row, column) not in injected and dirty.rows[row - 1][column] != clean.rows[clean_row - 1][column]:
                raise BenchmarkError("benchmark_truth_invalid", "脏表存在未记录在真值中的改动。")
    return aligned


def _load_run(directory: Path, dirty: Table) -> tuple[dict, Table, Table]:
    for name in (*OUTPUT_NAMES, "run_manifest.json"):
        _read_artifact(directory / name)
    try:
        manifest = _load_manifest(directory)
    except EngineError as exc:
        raise BenchmarkError(exc.code, exc.message) from None
    for name in OUTPUT_NAMES:
        if _sha256(_read_artifact(directory / name)) != manifest["outputs"][name]:
            raise BenchmarkError("run_hash_mismatch", "清洗输出文件的哈希与运行清单不符。")
    if (
        manifest["input"]["sha256"] != dirty.sha256
        or manifest["input"]["columns"] != dirty.columns
        or manifest["input"]["row_count"] != len(dirty.rows)
    ):
        raise BenchmarkError("run_input_mismatch", "清洗运行未绑定本次脏表或行数不一致。")
    try:
        cleaned = read_csv(directory / "cleaned.csv", encoding="utf-8")
        changes = read_csv(directory / "changes.csv", encoding="utf-8")
        flags = read_csv(directory / "flags.csv", encoding="utf-8")
    except CSVError as exc:
        raise BenchmarkError(exc.code, exc.message) from None
    if len(cleaned.rows) != len(dirty.rows):
        raise BenchmarkError("run_row_count_mismatch", "清洗结果行数必须与脏表一致，不能删除或插入行。")
    if changes.columns != CHANGE_COLUMNS or flags.columns != FLAG_COLUMNS:
        raise BenchmarkError("run_records_invalid", "变更记录或标记记录的表头不正确。")
    for records in (changes.rows, flags.rows):
        for item in records:
            if (
                re.fullmatch(r"[1-9][0-9]*", item["row"]) is None
                or len(item["row"]) > len(str(len(cleaned.rows)))
                or int(item["row"]) > len(cleaned.rows)
                or item["column"] not in cleaned.columns
            ):
                raise BenchmarkError("run_records_invalid", "变更或标记引用了不存在的行或列。")
    return manifest, cleaned, flags


def _column_mapping(path: Path, original: list[str], output: list[str]) -> dict[str, str]:
    supplied = _json_file(path)
    if not isinstance(supplied, dict) or any(
        key not in original or not isinstance(value, str) or value not in output
        for key, value in supplied.items()
    ):
        raise BenchmarkError("column_map_invalid", "列映射引用了不存在的标准列或输出列。")
    mapping = {column: supplied.get(column, column) for column in original}
    if any(value not in output for value in mapping.values()) or len(set(mapping.values())) != len(mapping):
        raise BenchmarkError("column_map_invalid", "每个标准列必须对应一个存在且不重叠的输出列。")
    return mapping


def _score(clean: Table, dirty: Table, cleaned: Table, flags: Table, truth: dict,
           aligned: dict[int, int], mapping: dict[str, str]) -> tuple[dict, int, int, int]:
    flagged = set()
    duplicate_rows = set()
    names_to_original = {target: original for original, target in mapping.items()}
    for original, target in mapping.items():
        names_to_original.setdefault(original, original)
    for item in flags.rows:
        row = int(item["row"])
        if item["flag_code"] == "duplicate_key":
            duplicate_rows.add(row)
        column = names_to_original.get(item["column"])
        if column is not None:
            flagged.add((row, column))
    by_error = {
        name: {"injected": 0, "corrected": 0, "wrong": 0, "missed": 0, "flagged": 0}
        for name in ERROR_TYPES
    }
    injected = {(item["row"], item["column"]): item for item in truth["errors"]}
    false_changes = false_positives = unmodified = 0
    for row_number in range(1, len(dirty.rows) + 1):
        original = clean.rows[aligned[row_number] - 1]
        actual = cleaned.rows[row_number - 1]
        for column in clean.columns:
            value = actual[mapping[column]]
            item = injected.get((row_number, column))
            has_flag = (row_number, column) in flagged
            if item is None:
                unmodified += 1
                false_changes += value != original[column]
                false_positives += has_flag
                continue
            counts = by_error[item["error_type"]]
            counts["injected"] += 1
            expected = item["expected"]
            if expected == "restore":
                counts["corrected" if value == item["clean_value"]
                       else "missed" if value == item["dirty_value"] else "wrong"] += 1
            elif expected == "to_missing":
                counts["corrected" if value == "" and has_flag else "missed"] += 1
            else:
                detected = row_number in duplicate_rows if item["error_type"] == "duplicate_record" else has_flag
                counts["flagged" if detected else "missed"] += 1
    return by_error, false_changes, false_positives, unmodified


def evaluate_benchmark(bench_dir: str | Path, run_dir: str | Path,
                       column_map: str | Path, output_dir: str | Path, *,
                       project_root: str | Path | None = None) -> dict:
    target = validate_target(output_dir, project_root)
    benchmark_path = project_path(bench_dir, project_root)
    run_path = project_path(run_dir, project_root)
    manifest, clean, dirty, truth, aligned = _load_benchmark(benchmark_path)
    run, cleaned, flags = _load_run(run_path, dirty)
    mapping = _column_mapping(project_path(column_map, project_root), clean.columns, cleaned.columns)
    by_error, false_changes, false_positives, unmodified = _score(
        clean, dirty, cleaned, flags, truth, aligned, mapping,
    )
    result = {
        "schema_version": "cleaning-evaluation/1",
        "by_error": by_error,
        "false_changes": false_changes,
        "false_positives": false_positives,
        "bench_hash": manifest["bench_hash"],
        "run_hash": sha256_json({
            "run_id": run["run_id"], "input_sha256": run["input"]["sha256"],
            "recipe_sha256": run["recipe"]["sha256"], "outputs": run["outputs"],
        }),
        "counts": {
            "clean_rows": len(clean.rows), "dirty_rows": len(dirty.rows),
            "columns": len(clean.columns), "unmodified_cells": unmodified,
        },
    }
    result["evaluation_hash"] = sha256_json(result)
    # This is a fixed benchmark marker, not the wall-clock evaluation time.
    result["created_at_utc"] = FIXED_TIMESTAMP
    details = "；".join(
        f"{_LABELS[name]}注入 {counts['injected']}，修对或处理对 {counts['corrected']}，"
        f"改错 {counts['wrong']}，漏掉 {counts['missed']}，标出 {counts['flagged']}"
        for name, counts in by_error.items()
    )
    summary = (
        f"本次合成评测将 {len(dirty.rows)} 行脏记录对齐到 {len(clean.rows)} 行标准答案。"
        f"{details}。未注入错误的单元格中，误改 {false_changes} 个，误报 {false_positives} 个。"
        "结果只用于评测，不代表真实数据的临床判定。\n"
    )
    publish_files(target, {
        "evaluation.json": json_bytes(result),
        "summary.txt": summary.encode("utf-8"),
    }, project_root=project_root)
    return result
