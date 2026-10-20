from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from researchops_clean.bench.common import FIXED_TIMESTAMP, BenchmarkError, json_bytes
from researchops_clean.bench.evaluate import evaluate_benchmark
from researchops_clean.bench.generate import generate_benchmark
from researchops_clean.csv_io import csv_bytes, read_csv
from researchops_clean.engine import (
    CHANGE_COLUMNS,
    FLAG_COLUMNS,
    _make_manifest,
    _run_id,
    apply_recipe,
    verify_run,
)
from researchops_clean.ledger import sha256_json


COLUMNS = ["住院号", "出生日期", "性别", "采样时间", "项目", "结果", "单位", "年龄"]
ERROR_TYPES = (
    "date_format", "sex_code", "item_alias", "unit_variant",
    "fullwidth_space", "sentinel", "impossible_value", "duplicate_record",
)
BENCH_FILES = (
    "clean.csv", "dirty.csv", "truth.json", "reference_recipe.json",
    "reference_column_map.json", "README.md",
)
RUN_FILES = ("cleaned.csv", "changes.csv", "flags.csv", "run_manifest.json")


def baseline_rows():
    return [
        dict(zip(COLUMNS, ("0123456", "2020-01-01", "男", "2023-05-06", "血红蛋白", "112", "g/L", "3岁4月"))),
        dict(zip(COLUMNS, ("0234567", "2022-01-01", "女", "2023-05-07", "血糖", "5.4", "mmol/L", "1岁4月"))),
        dict(zip(COLUMNS, ("0345678", "2021-01-01", "男", "2023-05-08", "白细胞计数", "6.2", "×10^9/L", "2岁4月"))),
    ]


def flag(row, column, code="synthetic_flag"):
    return {"row": str(row), "column": column, "flag_code": code, "step_id": "S1", "detail": "synthetic"}


class BenchmarkEvaluationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bench = self.root / "artifacts" / "bench" / "input"
        self.run = self.root / "artifacts" / "cleaning" / "run"
        self.output = self.root / "artifacts" / "bench" / "evaluation"
        self.column_map = self.root / "column_map.json"
        self.column_map.write_bytes(json_bytes({}))

    def make_benchmark(self, errors=(), *, row_mapping=None):
        """Build tiny known answers independently of the random generator."""
        self.bench.mkdir(parents=True)
        self.clean = baseline_rows()
        if row_mapping is None:
            row_mapping = [
                {"dirty_row": i, "clean_row": i, "is_duplicate": False, "duplicate_group": None}
                for i in range(1, len(self.clean) + 1)
            ]
        self.dirty = [copy.deepcopy(self.clean[item["clean_row"] - 1]) for item in row_mapping]
        truth_errors = []
        for row, column, dirty_value, error_type, expected in errors:
            clean_row = row_mapping[row - 1]["clean_row"]
            truth_errors.append({
                "row": row, "column": column,
                "clean_value": self.clean[clean_row - 1][column],
                "dirty_value": dirty_value, "error_type": error_type, "expected": expected,
            })
            self.dirty[row - 1][column] = dirty_value
        truth = {
            "schema_version": "benchmark-truth/1",
            "columns": COLUMNS,
            "row_mapping": row_mapping,
            "errors": truth_errors,
        }
        self.bench.joinpath("clean.csv").write_bytes(csv_bytes(COLUMNS, self.clean))
        self.bench.joinpath("dirty.csv").write_bytes(csv_bytes(COLUMNS, self.dirty))
        self.bench.joinpath("truth.json").write_bytes(json_bytes(truth))
        recipe = {
            "schema_version": "cleaning-recipe/1",
            "input": {"sha256": read_csv(self.bench / "dirty.csv").sha256, "columns": COLUMNS},
            "steps": [],
        }
        self.bench.joinpath("reference_recipe.json").write_bytes(json_bytes(recipe))
        self.bench.joinpath("reference_column_map.json").write_bytes(json_bytes({}))
        self.bench.joinpath("README.md").write_bytes("合成评测专用，真实数据必须由人批准。\n".encode("utf-8"))
        manifest = {
            "schema_version": "cleaning-benchmark/1",
            "generator_version": "1",
            "parameters": {"rows": len(self.clean), "seed": 7, "rates": {name: 0 for name in ERROR_TYPES}},
            "seed": 7,
            "files": {name: hashlib.sha256((self.bench / name).read_bytes()).hexdigest() for name in BENCH_FILES},
            "error_counts": {name: sum(item["error_type"] == name for item in truth_errors) for name in ERROR_TYPES},
        }
        manifest["bench_hash"] = sha256_json(manifest)
        self.bench.joinpath("bench_manifest.json").write_bytes(json_bytes(manifest))
        return truth

    def make_run(self, rows=None, *, flags=(), columns=None):
        """Hash a hand-written result; deliberately do not replay a recipe."""
        self.run.mkdir(parents=True)
        rows = copy.deepcopy(self.dirty if rows is None else rows)
        columns = list(COLUMNS if columns is None else columns)
        outputs = {
            "cleaned.csv": csv_bytes(columns, rows),
            "changes.csv": csv_bytes(CHANGE_COLUMNS, []),
            "flags.csv": csv_bytes(FLAG_COLUMNS, list(flags)),
        }
        recipe = json.loads(self.bench.joinpath("reference_recipe.json").read_text(encoding="utf-8"))
        manifest = _make_manifest(
            read_csv(self.bench / "dirty.csv"), recipe, outputs, [], {"formula_like_cells": 0},
        )
        manifest["created_at_utc"] = "2000-01-01T00:00:00Z"
        for name, data in outputs.items():
            self.run.joinpath(name).write_bytes(data)
        self.run.joinpath("run_manifest.json").write_bytes(json_bytes(manifest))

    def evaluate(self, *, output=None):
        return evaluate_benchmark(
            self.bench, self.run, self.column_map, output or self.output, project_root=self.root,
        )

    def rehash_benchmark(self):
        path = self.bench / "bench_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        for name in BENCH_FILES:
            manifest["files"][name] = hashlib.sha256((self.bench / name).read_bytes()).hexdigest()
        manifest.pop("bench_hash", None)
        manifest["bench_hash"] = sha256_json(manifest)
        path.write_bytes(json_bytes(manifest))

    def rewrite_run_manifest(self, mutate):
        path = self.run / "run_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        mutate(manifest)
        manifest["run_id"] = _run_id(manifest)
        path.write_bytes(json_bytes(manifest))

    def assert_rejected(self):
        with self.assertRaises(BenchmarkError) as failure:
            self.evaluate()
        self.assertTrue(failure.exception.code)
        self.assertNotIn(str(self.root), failure.exception.message)
        self.assertFalse(self.output.exists())

    def test_restore_correct_wrong_and_unchanged_are_separate_categories(self):
        self.make_benchmark([
            (1, "性别", "M", "sex_code", "restore"),
            (2, "性别", "F", "sex_code", "restore"),
            (3, "性别", "1", "sex_code", "restore"),
        ])
        cleaned = copy.deepcopy(self.dirty)
        cleaned[0]["性别"] = "男"
        cleaned[1]["性别"] = "男"
        self.make_run(cleaned)
        result = self.evaluate()
        self.assertEqual(result["by_error"]["sex_code"],
                         {"injected": 3, "corrected": 1, "wrong": 1, "missed": 1, "flagged": 0})
        self.assertEqual(result["false_changes"], 0)
        self.assertEqual(result["false_positives"], 0)
        self.assertEqual(result["counts"], {
            "clean_rows": 3, "dirty_rows": 3, "columns": 8, "unmodified_cells": 21,
        })
        self.assertEqual(result["schema_version"], "cleaning-evaluation/1")
        self.assertEqual(set(result["by_error"]), set(ERROR_TYPES))
        self.assertEqual(json.loads((self.output / "evaluation.json").read_text(encoding="utf-8")), result)
        self.assertTrue((self.output / "summary.txt").read_text(encoding="utf-8").strip())

    def test_to_missing_requires_both_empty_value_and_matching_flag(self):
        self.make_benchmark([
            (1, "年龄", "999", "sentinel", "to_missing"),
            (2, "年龄", "999", "sentinel", "to_missing"),
            (3, "年龄", "999", "sentinel", "to_missing"),
        ])
        cleaned = copy.deepcopy(self.dirty)
        cleaned[0]["年龄"] = ""
        cleaned[1]["年龄"] = ""
        self.make_run(cleaned, flags=[flag(1, "年龄", "sentinel_missing"), flag(3, "年龄", "sentinel_missing")])
        result = self.evaluate()
        self.assertEqual(result["by_error"]["sentinel"],
                         {"injected": 3, "corrected": 1, "wrong": 0, "missed": 2, "flagged": 0})
        self.assertEqual(result["false_changes"], 0)

    def test_flag_only_does_not_require_changing_the_impossible_value(self):
        self.make_benchmark([
            (1, "结果", "1120", "impossible_value", "flag_only"),
            (2, "结果", "9999", "impossible_value", "flag_only"),
        ])
        self.make_run(flags=[flag(1, "结果", "out_of_range"), flag(2, "单位", "out_of_range")])
        result = self.evaluate()
        self.assertEqual(result["by_error"]["impossible_value"],
                         {"injected": 2, "corrected": 0, "wrong": 0, "missed": 1, "flagged": 1})
        self.assertEqual(result["false_positives"], 1)

    def test_unmodified_cells_count_false_changes_and_deduplicated_false_positives(self):
        self.make_benchmark()
        cleaned = copy.deepcopy(self.dirty)
        cleaned[0]["性别"] = "女"
        cleaned[1]["项目"] = "血红蛋白"
        self.make_run(cleaned, flags=[
            flag(1, "性别"), flag(1, "性别", "second_flag"),
            flag(3, "单位"),
        ])
        result = self.evaluate()
        self.assertEqual(result["false_changes"], 2)
        self.assertEqual(result["false_positives"], 2)
        self.assertEqual(result["counts"]["unmodified_cells"], 24)
        self.assertTrue(all(item["injected"] == 0 for item in result["by_error"].values()))

    def test_column_map_and_original_or_target_flags_share_the_same_cell(self):
        self.make_benchmark([(1, "年龄", "999", "sentinel", "to_missing")])
        columns = COLUMNS + ["清洗年龄"]
        cleaned = [dict(row, 清洗年龄=row["年龄"]) for row in self.dirty]
        cleaned[0]["清洗年龄"] = ""
        self.column_map.write_bytes(json_bytes({"年龄": "清洗年龄"}))
        self.make_run(cleaned, columns=columns, flags=[
            flag(1, "年龄", "sentinel_missing"),
            flag(1, "清洗年龄", "sentinel_missing"),
            flag(2, "年龄"), flag(2, "清洗年龄"),
        ])
        result = self.evaluate()
        self.assertEqual(result["by_error"]["sentinel"]["corrected"], 1)
        self.assertEqual(result["false_positives"], 1)
        self.assertEqual(result["false_changes"], 0)

    def test_swapped_standard_column_targets_assign_flags_only_to_target_owner(self):
        self.make_benchmark()
        cleaned = copy.deepcopy(self.dirty)
        for row in cleaned:
            row["性别"], row["项目"] = row["项目"], row["性别"]
        self.column_map.write_bytes(json_bytes({"性别": "项目", "项目": "性别"}))
        self.make_run(cleaned, flags=[flag(1, "项目")])
        result = self.evaluate()
        self.assertEqual(result["false_changes"], 0)
        self.assertEqual(result["false_positives"], 1)

    def test_row_mapping_aligns_reordered_originals_and_inserted_copies(self):
        mapping = [
            {"dirty_row": 1, "clean_row": 3, "is_duplicate": False, "duplicate_group": None},
            {"dirty_row": 2, "clean_row": 1, "is_duplicate": False, "duplicate_group": 1},
            {"dirty_row": 3, "clean_row": 1, "is_duplicate": True, "duplicate_group": 1},
            {"dirty_row": 4, "clean_row": 2, "is_duplicate": False, "duplicate_group": None},
        ]
        self.make_benchmark([
            (2, "住院号", "0123456", "duplicate_record", "flag_only"),
            (3, "住院号", "0123456", "duplicate_record", "flag_only"),
            (3, "性别", "M", "sex_code", "restore"),
            (4, "采样时间", "20230507", "date_format", "restore"),
        ], row_mapping=mapping)
        cleaned = copy.deepcopy(self.dirty)
        cleaned[2]["性别"] = "男"
        cleaned[3]["采样时间"] = "2023-05-07"
        self.make_run(cleaned, flags=[
            flag(2, "项目", "duplicate_key"), flag(3, "采样时间", "duplicate_key"),
        ])
        result = self.evaluate()
        self.assertEqual(result["by_error"]["duplicate_record"],
                         {"injected": 2, "corrected": 0, "wrong": 0, "missed": 0, "flagged": 2})
        self.assertEqual(result["by_error"]["sex_code"]["corrected"], 1)
        self.assertEqual(result["by_error"]["date_format"]["corrected"], 1)
        self.assertEqual(result["false_changes"], 0)
        # The flagged key cells were not themselves injected, so count them.
        self.assertEqual(result["false_positives"], 2)
        self.assertEqual(result["counts"]["dirty_rows"], 4)

    def test_duplicate_requires_duplicate_key_and_marks_original_and_copy_separately(self):
        mapping = [
            {"dirty_row": 1, "clean_row": 1, "is_duplicate": False, "duplicate_group": 1},
            {"dirty_row": 2, "clean_row": 2, "is_duplicate": False, "duplicate_group": None},
            {"dirty_row": 3, "clean_row": 3, "is_duplicate": False, "duplicate_group": None},
            {"dirty_row": 4, "clean_row": 1, "is_duplicate": True, "duplicate_group": 1},
        ]
        self.make_benchmark([
            (1, "住院号", "0123456", "duplicate_record", "flag_only"),
            (4, "住院号", "0123456", "duplicate_record", "flag_only"),
        ], row_mapping=mapping)
        self.make_run(flags=[flag(1, "住院号", "duplicate_key"), flag(4, "住院号", "out_of_range")])
        result = self.evaluate()
        self.assertEqual(result["by_error"]["duplicate_record"]["flagged"], 1)
        self.assertEqual(result["by_error"]["duplicate_record"]["missed"], 1)

    def test_run_files_must_all_exist(self):
        self.make_benchmark()
        self.make_run()
        for name in RUN_FILES:
            with self.subTest(name=name):
                path = self.run / name
                original = path.read_bytes()
                path.unlink()
                self.assert_rejected()
                path.write_bytes(original)

    def test_benchmark_files_must_all_exist(self):
        self.make_benchmark()
        self.make_run()
        for name in (*BENCH_FILES, "bench_manifest.json"):
            with self.subTest(name=name):
                path = self.bench / name
                original = path.read_bytes()
                path.unlink()
                self.assert_rejected()
                path.write_bytes(original)

    def test_each_run_output_hash_and_manifest_identity_are_checked(self):
        self.make_benchmark()
        self.make_run()
        for name in RUN_FILES:
            with self.subTest(name=name):
                path = self.run / name
                original = path.read_bytes()
                if name == "run_manifest.json":
                    value = json.loads(original)
                    value["run_id"] = "C-000000000000"
                    path.write_bytes(json_bytes(value))
                else:
                    path.write_bytes(original + b"synthetic tampering\n")
                self.assert_rejected()
                path.write_bytes(original)

    def test_each_benchmark_file_hash_and_bench_identity_are_checked(self):
        self.make_benchmark()
        self.make_run()
        for name in (*BENCH_FILES, "bench_manifest.json"):
            with self.subTest(name=name):
                path = self.bench / name
                original = path.read_bytes()
                if name == "bench_manifest.json":
                    value = json.loads(original)
                    value["bench_hash"] = "0" * 64
                    path.write_bytes(json_bytes(value))
                else:
                    path.write_bytes(original + b"synthetic tampering\n")
                self.assert_rejected()
                path.write_bytes(original)

    def test_run_is_bound_to_this_dirty_input(self):
        self.make_benchmark()
        self.make_run()
        self.rewrite_run_manifest(lambda manifest: manifest["input"].update({"sha256": "0" * 64}))
        self.assert_rejected()

    def test_run_row_count_matches_dirty_table_and_cleaned_rows(self):
        self.make_benchmark()
        self.make_run(self.dirty[:-1])
        self.assert_rejected()
        self.run.joinpath("cleaned.csv").write_bytes(csv_bytes(COLUMNS, self.dirty))
        self.rewrite_run_manifest(lambda manifest: (
            manifest["outputs"].update({"cleaned.csv": hashlib.sha256((self.run / "cleaned.csv").read_bytes()).hexdigest()}),
            manifest["input"].update({"row_count": len(self.dirty) + 1}),
        ))
        self.assert_rejected()

    def test_column_map_requires_known_source_existing_unique_targets(self):
        self.make_benchmark()
        self.make_run()
        for mapping in (
            {"不存在": "性别"}, {"性别": "不存在"},
            {"性别": "项目"}, {"性别": "项目", "结果": "项目"},
            {"性别": 1}, [], {"性别": ""},
        ):
            with self.subTest(mapping=mapping):
                self.column_map.write_bytes(json_bytes(mapping))
                self.assert_rejected()

    def test_missing_default_column_is_rejected_even_with_valid_hash(self):
        self.make_benchmark()
        columns = COLUMNS[:-1]
        self.make_run([{key: row[key] for key in columns} for row in self.dirty], columns=columns)
        self.assert_rejected()

    def test_flags_require_an_existing_output_column_and_valid_dirty_row(self):
        self.make_benchmark()
        self.make_run()
        for record in (flag(1, "不存在"), flag(0, "性别"), flag(4, "性别")):
            with self.subTest(record=record):
                data = csv_bytes(FLAG_COLUMNS, [record])
                self.run.joinpath("flags.csv").write_bytes(data)
                self.rewrite_run_manifest(lambda manifest: manifest["outputs"].update(
                    {"flags.csv": hashlib.sha256(data).hexdigest()}))
                self.assert_rejected()

    def test_extremely_long_change_and_flag_row_numbers_fail_with_controlled_error(self):
        self.make_benchmark()
        self.make_run()
        long_row = "9" * 5000
        cases = (
            ("changes.csv", CHANGE_COLUMNS, {
                "row": long_row, "column": "性别", "old_value": "男",
                "new_value": "女", "step_id": "S1", "op": "map_values",
            }),
            ("flags.csv", FLAG_COLUMNS, flag(long_row, "性别")),
        )
        for name, columns, record in cases:
            with self.subTest(name=name):
                path = self.run / name
                original = path.read_bytes()
                data = csv_bytes(columns, [record])
                path.write_bytes(data)
                self.rewrite_run_manifest(lambda manifest: manifest["outputs"].update(
                    {name: hashlib.sha256(data).hexdigest()}))
                try:
                    with self.assertRaises(BenchmarkError) as failure:
                        self.evaluate()
                    self.assertEqual(failure.exception.code, "run_records_invalid")
                    self.assertNotIn(str(self.root), failure.exception.message)
                    self.assertFalse(self.output.exists())
                finally:
                    path.write_bytes(original)
                    self.rewrite_run_manifest(lambda manifest: manifest["outputs"].update(
                        {name: hashlib.sha256(original).hexdigest()}))

    def test_flags_on_unmapped_extra_columns_do_not_change_standard_cell_scores(self):
        self.make_benchmark()
        columns = COLUMNS + ["附加列"]
        self.make_run([dict(row, 附加列="附加") for row in self.dirty],
                      columns=columns, flags=[flag(1, "附加列")])
        result = self.evaluate()
        self.assertEqual(result["false_changes"], 0)
        self.assertEqual(result["false_positives"], 0)

    def test_truth_row_mapping_must_be_complete_unique_and_in_bounds(self):
        self.make_benchmark()
        self.make_run()
        path = self.bench / "truth.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            lambda truth: truth["row_mapping"].pop(),
            lambda truth: truth["row_mapping"][1].update({"dirty_row": 1}),
            lambda truth: truth["row_mapping"][0].update({"clean_row": 0}),
            lambda truth: truth["row_mapping"][0].update({"clean_row": 4}),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(original)
                mutate(changed)
                path.write_bytes(json_bytes(changed))
                self.rehash_benchmark()
                self.assert_rejected()

    def test_truth_error_cells_and_values_must_match_benchmark(self):
        self.make_benchmark([(1, "性别", "M", "sex_code", "restore")])
        self.make_run()
        path = self.bench / "truth.json"
        original = json.loads(path.read_text(encoding="utf-8"))
        mutations = (
            lambda truth: truth["errors"][0].update({"dirty_value": "F"}),
            lambda truth: truth["errors"][0].update({"clean_value": "女"}),
            lambda truth: truth["errors"][0].update({"row": 4}),
            lambda truth: truth["errors"][0].update({"column": "不存在"}),
            lambda truth: truth["errors"].append(copy.deepcopy(truth["errors"][0])),
        )
        for index, mutate in enumerate(mutations):
            with self.subTest(index=index):
                changed = copy.deepcopy(original)
                mutate(changed)
                path.write_bytes(json_bytes(changed))
                self.rehash_benchmark()
                self.assert_rejected()

    def test_existing_output_is_not_overwritten(self):
        self.make_benchmark()
        self.make_run()
        self.output.mkdir(parents=True)
        marker = self.output / "keep.txt"
        marker.write_bytes(b"keep")
        with self.assertRaises(BenchmarkError):
            self.evaluate()
        self.assertEqual(marker.read_bytes(), b"keep")
        self.assertFalse(self.output.joinpath("evaluation.json").exists())

    def test_identical_inputs_produce_identical_files_and_timestamp_is_excluded_from_hash(self):
        self.make_benchmark([(1, "性别", "M", "sex_code", "restore")])
        self.make_run()
        first = self.evaluate()
        second_output = self.output.with_name("evaluation-again")
        second = self.evaluate(output=second_output)
        self.assertEqual(first, second)
        self.assertEqual(first["created_at_utc"], FIXED_TIMESTAMP)
        for name in ("evaluation.json", "summary.txt"):
            self.assertEqual((self.output / name).read_bytes(), (second_output / name).read_bytes())
        identity = {key: value for key, value in first.items() if key not in {"created_at_utc", "evaluation_hash"}}
        self.assertEqual(first["evaluation_hash"], sha256_json(identity))
        self.assertTrue(first["bench_hash"])
        self.assertTrue(first["run_hash"])

    def test_reference_recipe_full_pipeline_meets_every_required_score(self):
        generate_benchmark(rows=200, seed=7, output_dir=self.bench, project_root=self.root)
        apply_recipe(self.bench / "dirty.csv", self.bench / "reference_recipe.json", self.run, project_root=self.root)
        verified = verify_run(self.bench / "dirty.csv", self.bench / "reference_recipe.json", self.run, project_root=self.root)
        self.assertEqual(verified.exit_code, 0, verified.message)
        self.column_map = self.bench / "reference_column_map.json"
        result = self.evaluate()
        truth = json.loads(self.bench.joinpath("truth.json").read_text(encoding="utf-8"))
        expectations = {item["error_type"]: item["expected"] for item in truth["errors"]}
        self.assertEqual(set(expectations), set(ERROR_TYPES))
        for kind, expected in expectations.items():
            with self.subTest(kind=kind):
                counts = result["by_error"][kind]
                self.assertGreater(counts["injected"], 0)
                self.assertEqual(counts["corrected" if expected in {"restore", "to_missing"} else "flagged"], counts["injected"])
                self.assertEqual(counts["wrong"], 0)
                self.assertEqual(counts["missed"], 0)
        self.assertEqual(result["false_changes"], 0)


if __name__ == "__main__":
    unittest.main()
