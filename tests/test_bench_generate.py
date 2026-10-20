"""Synthetic benchmark generation contracts without models or network access."""
from __future__ import annotations

import copy
import hashlib
import json
import re
import tempfile
import unittest
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from pathlib import Path
from unittest.mock import patch

from researchops_clean.bench.common import BenchmarkError, FIXED_TIMESTAMP
from researchops_clean.bench.generate import (
    DEFAULT_RATES, ERROR_TYPES, generate_benchmark,
)
from researchops_clean.bench.reference import (
    COLUMNS, PROJECT_CONFIG, REFERENCE_COLUMN_MAP, build_reference_recipe, fixed_value,
)
from researchops_clean.csv_io import read_csv
from researchops_clean.ledger import sha256_json
from researchops_clean.recipe import validate_steps
from researchops_clean.sensitive_patterns import find_sensitive_types


FILES = {
    "clean.csv", "dirty.csv", "truth.json", "reference_recipe.json",
    "reference_column_map.json", "README.md", "bench_manifest.json",
}


class BenchmarkGenerationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()
        self.target = self.root / "artifacts" / "bench"

    def generate(self, target=None, **kwargs):
        return generate_benchmark(
            rows=kwargs.pop("rows", 200), seed=kwargs.pop("seed", 7),
            output_dir=target or self.target, project_root=self.root, **kwargs,
        )

    def load(self, name, target=None):
        return json.loads(((target or self.target) / name).read_text(encoding="utf-8"))

    def test_same_seed_all_files_identical_different_seed_changes_dirty(self):
        self.generate()
        second, third = self.target.with_name("same"), self.target.with_name("different")
        self.generate(second)
        self.generate(third, seed=8)
        self.assertEqual({path.name for path in self.target.iterdir()}, FILES)
        for name in FILES:
            self.assertEqual((self.target / name).read_bytes(), (second / name).read_bytes(), name)
        self.assertNotEqual((self.target / "dirty.csv").read_bytes(), (third / "dirty.csv").read_bytes())

    def test_clean_rows_obey_patient_date_project_unit_and_precision_contracts(self):
        self.generate()
        table = read_csv(self.target / "clean.csv")
        self.assertEqual(table.columns, COLUMNS)
        self.assertEqual(len(table.rows), 200)
        patients = defaultdict(list)
        for row in table.rows:
            self.assertRegex(row["住院号"], r"^0[0-9]{6}$")
            self.assertIn(row["性别"], ("男", "女"))
            birth = date.fromisoformat(row["出生日期"])
            sampled = date.fromisoformat(row["采样时间"])
            self.assertGreater(sampled, birth)
            years = sampled.year - birth.year - ((sampled.month, sampled.day) < (birth.month, birth.day))
            self.assertTrue(0 <= years <= 14)
            self.assertRegex(row["年龄"], r"^(?:[0-9]+岁)?(?:[0-9]+月)?(?:[0-9]+天)?$")
            self.assertTrue(row["年龄"])
            parsed_years = re.match(r"([0-9]+)岁", row["年龄"])
            self.assertEqual(int(parsed_years.group(1)) if parsed_years else 0, years)
            config = PROJECT_CONFIG[row["项目"]]
            self.assertEqual(row["单位"], config["unit"])
            self.assertRegex(row["结果"], r"^[0-9]+\.[0-9]$")
            tenths = Decimal(row["结果"]) * 10
            self.assertGreaterEqual(tenths, config["min_tenths"])
            self.assertLessEqual(tenths, config["max_tenths"])
            patients[row["住院号"]].append(row)
        self.assertEqual(len(patients), 50)
        for records in patients.values():
            self.assertEqual(len(records), 4)
            self.assertEqual(len({row["出生日期"] for row in records}), 1)
            self.assertEqual(len({row["性别"] for row in records}), 1)
            self.assertEqual({row["项目"] for row in records}, set(PROJECT_CONFIG))
        self.assertEqual(len({tuple(row[column] for column in COLUMNS) for row in table.rows}), 200)

    def test_truth_cells_align_to_both_tables_and_never_overlap(self):
        manifest = self.generate()
        clean = read_csv(self.target / "clean.csv").rows
        dirty = read_csv(self.target / "dirty.csv").rows
        truth = self.load("truth.json")
        self.assertEqual(truth["schema_version"], "benchmark-truth/1")
        self.assertEqual(truth["columns"], COLUMNS)
        self.assertEqual(len(truth["row_mapping"]), len(dirty))
        self.assertEqual(
            [entry["dirty_row"] for entry in truth["row_mapping"]], list(range(1, len(dirty) + 1)),
        )
        mapping = {entry["dirty_row"]: entry for entry in truth["row_mapping"]}
        self.assertEqual(
            sorted(entry["clean_row"] for entry in truth["row_mapping"] if not entry["is_duplicate"]),
            list(range(1, len(clean) + 1)),
        )
        seen = set()
        for error in truth["errors"]:
            cell = (error["row"], error["column"])
            self.assertNotIn(cell, seen)
            seen.add(cell)
            clean_row = mapping[error["row"]]["clean_row"]
            self.assertEqual(error["dirty_value"], dirty[error["row"] - 1][error["column"]])
            self.assertEqual(error["clean_value"], clean[clean_row - 1][error["column"]])
            self.assertIn(error["error_type"], ERROR_TYPES)
            self.assertEqual(error["expected"], {
                "sentinel": "to_missing", "impossible_value": "flag_only",
                "duplicate_record": "flag_only",
            }.get(error["error_type"], "restore"))
            if error["expected"] == "restore":
                self.assertNotEqual(error["dirty_value"], error["clean_value"])
        for index, row in enumerate(dirty, 1):
            clean_row = clean[mapping[index]["clean_row"] - 1]
            for column in COLUMNS:
                if (index, column) not in seen:
                    self.assertEqual(row[column], clean_row[column])
        counts = Counter(error["error_type"] for error in truth["errors"])
        self.assertEqual(manifest["error_counts"], {name: counts[name] for name in ERROR_TYPES})
        self.assertTrue(all(manifest["error_counts"][name] > 0 for name in ERROR_TYPES))

    def test_duplicate_groups_keep_original_and_copy_and_mark_every_cell(self):
        manifest = self.generate()
        truth = self.load("truth.json")
        groups = defaultdict(list)
        for entry in truth["row_mapping"]:
            if entry["duplicate_group"] is not None:
                groups[entry["duplicate_group"]].append(entry)
        self.assertEqual(len(groups), manifest["event_counts"]["duplicate_record"])
        by_row = defaultdict(list)
        for error in truth["errors"]:
            by_row[error["row"]].append(error)
        for entries in groups.values():
            self.assertEqual(len(entries), 2)
            self.assertEqual({entry["is_duplicate"] for entry in entries}, {False, True})
            self.assertEqual(len({entry["clean_row"] for entry in entries}), 1)
            for entry in entries:
                errors = by_row[entry["dirty_row"]]
                self.assertEqual({error["column"] for error in errors}, set(COLUMNS))
                expected_duplicates = 7 if entry["is_duplicate"] else 8
                self.assertEqual(
                    sum(error["error_type"] == "duplicate_record" for error in errors),
                    expected_duplicates,
                )

    def test_manifest_protects_all_six_files_including_readme_and_itself_by_object_hash(self):
        manifest = self.generate()
        self.assertEqual(manifest, self.load("bench_manifest.json"))
        self.assertEqual(set(manifest["files"]), FILES - {"bench_manifest.json"})
        for name, expected in manifest["files"].items():
            self.assertEqual(hashlib.sha256((self.target / name).read_bytes()).hexdigest(), expected)
        hashed = copy.deepcopy(manifest)
        recorded = hashed.pop("bench_hash")
        self.assertEqual(sha256_json(hashed), recorded)
        self.assertEqual(manifest["parameters"], {"rows": 200, "seed": 7, "rates": DEFAULT_RATES})
        self.assertEqual(manifest["seed"], 7)

    def test_utf8_lf_outputs_have_no_local_paths_or_personal_identifiers(self):
        self.generate()
        for path in self.target.iterdir():
            raw = path.read_bytes()
            self.assertFalse(raw.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", raw)
            text = raw.decode("utf-8")
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.root.as_posix(), text)
        for name in ("clean.csv", "dirty.csv"):
            table = read_csv(self.target / name)
            self.assertFalse(set(table.columns) & {"姓名", "身份证号", "电话", "地址"})
            for row in table.rows:
                for value in row.values():
                    self.assertEqual(find_sensitive_types(value), [])

    def test_reference_has_fixed_approvals_warning_reasons_and_canonical_identity_maps(self):
        self.generate()
        recipe = self.load("reference_recipe.json")
        validate_steps(recipe)
        self.assertEqual(recipe["input"]["columns"], COLUMNS)
        self.assertEqual(recipe["input"]["sha256"],
                         hashlib.sha256((self.target / "dirty.csv").read_bytes()).hexdigest())
        for step in recipe["steps"]:
            self.assertTrue(step["reason"].startswith("【仅用于评测】"))
            self.assertEqual(step["approval"], {
                "status": "approved", "reviewer": "bench-reference",
                "decided_at_utc": FIXED_TIMESTAMP,
                "content_sha256": hashlib.sha256(json.dumps({
                    "input_sha256": recipe["input"]["sha256"],
                    "step_order": [item["step_id"] for item in recipe["steps"]],
                    "step_id": step["step_id"], "op": step["op"], "params": step["params"],
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False).encode("utf-8")).hexdigest(),
            })
        sex = next(step["params"]["mapping"] for step in recipe["steps"]
                   if step["op"] == "map_values" and step["params"]["column"] == "性别")
        self.assertEqual((sex["男"], sex["女"]), ("男", "女"))
        items = next(step["params"]["mapping"] for step in recipe["steps"]
                     if step["op"] == "map_values" and step["params"]["column"] == "项目")
        for item in PROJECT_CONFIG:
            self.assertEqual(items[item], item)
        self.assertEqual(self.load("reference_column_map.json"), REFERENCE_COLUMN_MAP)
        readme = (self.target / "README.md").read_text(encoding="utf-8")
        self.assertIn("仅用于评测", readme)
        self.assertIn("真实数据必须由人", readme)
        self.assertIn("不是医学参考区间", readme)

    def test_reference_grid_does_not_depend_on_generated_answers_or_seed(self):
        self.generate(rows=1, seed=7)
        second = self.target.with_name("different")
        self.generate(second, rows=200, seed=19)
        first_recipe = self.load("reference_recipe.json")
        second_recipe = self.load("reference_recipe.json", second)
        self.assertNotEqual(first_recipe["input"]["sha256"], second_recipe["input"]["sha256"])
        def approved_content(recipe):
            steps = copy.deepcopy(recipe["steps"])
            for step in steps:
                recorded = step["approval"].pop("content_sha256")
                self.assertEqual(recorded, hashlib.sha256(json.dumps({
                    "input_sha256": recipe["input"]["sha256"],
                    "step_order": [item["step_id"] for item in recipe["steps"]],
                    "step_id": step["step_id"], "op": step["op"], "params": step["params"],
                }, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                    allow_nan=False).encode("utf-8")).hexdigest())
            return steps

        self.assertEqual(approved_content(first_recipe), approved_content(second_recipe))
        for first, second_step in zip(first_recipe["steps"], second_recipe["steps"]):
            self.assertNotEqual(
                first["approval"]["content_sha256"], second_step["approval"]["content_sha256"],
            )
        mapping = next(step["params"]["mapping"] for step in first_recipe["steps"]
                       if step["op"] == "map_values" and step["params"]["column"] == "结果数值")
        for config in PROJECT_CONFIG.values():
            for tenths in range(config["min_tenths"], config["max_tenths"] + 1):
                fixed = fixed_value(tenths)
                canonical = fixed[:-2] if fixed.endswith(".0") else fixed
                self.assertEqual(mapping[fixed], fixed)
                self.assertEqual(mapping[canonical], fixed)
        recipe, column_map = build_reference_recipe("a" * 64)
        self.assertEqual(approved_content(recipe), approved_content(first_recipe))
        self.assertEqual(column_map, REFERENCE_COLUMN_MAP)

    def test_zero_rates_give_exact_clean_copy_and_duplicate_only_respects_zero_other_rates(self):
        rates = {name: 0 for name in ERROR_TYPES}
        manifest = self.generate(rates=rates)
        self.assertEqual((self.target / "clean.csv").read_bytes(), (self.target / "dirty.csv").read_bytes())
        self.assertEqual(self.load("truth.json")["errors"], [])
        self.assertTrue(all(count == 0 for count in manifest["error_counts"].values()))
        only_duplicates = {**rates, "duplicate_record": 0.05}
        second = self.target.with_name("duplicates")
        manifest = self.generate(second, rates=only_duplicates)
        self.assertEqual(manifest["event_counts"]["duplicate_record"], 10)
        self.assertEqual(manifest["error_counts"]["duplicate_record"], 160)
        self.assertTrue(all(manifest["error_counts"][name] == 0
                            for name in ERROR_TYPES if name != "duplicate_record"))

    def test_unit_injection_restores_both_numeric_and_unit_cells_and_covers_both_projects(self):
        rates = {name: 0 for name in ERROR_TYPES}
        rates["unit_variant"] = 1
        manifest = self.generate(rates=rates)
        self.assertEqual(manifest["event_counts"]["unit_variant"], 100)
        self.assertEqual(manifest["error_counts"]["unit_variant"], 200)
        dirty = read_csv(self.target / "dirty.csv").rows
        truth = self.load("truth.json")
        affected = defaultdict(list)
        for error in truth["errors"]:
            affected[error["row"]].append(error)
        self.assertEqual({dirty[index - 1]["项目"] for index in affected}, {"血红蛋白", "血糖"})
        for errors in affected.values():
            self.assertEqual({error["column"] for error in errors}, {"结果", "单位"})
            self.assertTrue(all(error["expected"] == "restore" for error in errors))

    def test_invalid_parameters_never_publish_and_defaults_are_not_mutated(self):
        invalid = [
            {"rows": 0}, {"rows": -1}, {"rows": 1.5}, {"rows": True},
            {"seed": True}, {"seed": "7"}, {"rates": []},
            {"rates": {"unknown": 0.1}}, {"rates": {"sentinel": -0.1}},
            {"rates": {"sentinel": 1.1}}, {"rates": {"sentinel": float("nan")}},
            {"rates": {"sentinel": float("inf")}}, {"rates": {"sentinel": True}},
        ]
        before = dict(DEFAULT_RATES)
        for parameters in invalid:
            with self.subTest(parameters=parameters):
                with self.assertRaises(BenchmarkError):
                    self.generate(**parameters)
                self.assertFalse(self.target.exists())
        self.assertEqual(DEFAULT_RATES, before)

    def test_global_random_functions_are_not_used(self):
        with patch("random.random", side_effect=AssertionError("global random used")), \
             patch("random.seed", side_effect=AssertionError("global random seeded")):
            self.generate()
        self.assertTrue((self.target / "bench_manifest.json").is_file())


if __name__ == "__main__":
    unittest.main()
