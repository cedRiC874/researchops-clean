from __future__ import annotations

import csv
import hashlib
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from researchops_clean import engine
from researchops_clean.engine import EngineError, apply_recipe, verify_run
from researchops_clean.ops import OperationError
from researchops_clean.paths import OutputDirectoryError, validate_output_directory
from researchops_clean.recipe import RecipeError


FIXTURES = Path(__file__).parent / "fixtures"
EXPECTED_CLEANED_SHA256 = "6b211043cf834eaaf3c5ba3d5bcb024411552231d89ff3f2674b4c003fa80d22"


def csv_rows(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


class RecipeEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.input = FIXTURES / "lab_example.csv"
        self.recipe = FIXTURES / "lab_recipe.json"
        self.target = self.artifacts / "run"

    def recipe_copy(self, mutate):
        value = json.loads(self.recipe.read_text(encoding="utf-8"))
        mutate(value)
        target = self.root / "recipe.json"
        target.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8", newline="\n")
        return target

    def apply(self, recipe=None, target=None, input_path=None, **kwargs):
        return apply_recipe(
            input_path or self.input, recipe or self.recipe, target or self.target,
            project_root=self.root, **kwargs,
        )

    def verify(self, **kwargs):
        return verify_run(self.input, self.recipe, self.target, project_root=self.root, **kwargs)

    def test_fixture_first_five_rows_match_every_acceptance_requirement(self):
        manifest = self.apply()
        rows = csv_rows(self.target / "cleaned.csv")
        flags = csv_rows(self.target / "flags.csv")
        changes = csv_rows(self.target / "changes.csv")
        self.assertEqual(len(rows), len(csv_rows(self.input)))
        self.assertGreaterEqual(len(rows), 10)
        first, second, third, fourth, fifth = rows[:5]
        self.assertEqual(first["住院号"], "0012345")
        self.assertEqual(first["采样日期"], "2023-05-06")
        self.assertEqual([first[name] for name in ("年龄_岁", "年龄_月", "年龄_天")], ["3", "2", "0"])
        self.assertEqual(first["结果数值"], "112")
        self.assertEqual(first["单位"], "g/L")
        self.assertEqual(second["性别"], "男")
        self.assertEqual(second["项目"], "血红蛋白")
        self.assertEqual(second["采样日期"], "2023-05-06")
        self.assertEqual(second["结果数值"], "112")
        self.assertEqual(second["单位"], "g/L")
        duplicate_flags = {
            item["row"]: item for item in flags if item["flag_code"] == "duplicate_key"
        }
        self.assertIn("1", duplicate_flags)
        self.assertIn("2", duplicate_flags)
        self.assertEqual(duplicate_flags["1"]["detail"], duplicate_flags["2"]["detail"])
        self.assertEqual(third["采样日期"], "2023-05-07")
        self.assertEqual([third[name] for name in ("年龄_岁", "年龄_月", "年龄_天")], ["0", "0", "15"])
        self.assertEqual(third["结果数值"], "0.5")
        self.assertEqual(third["结果限定"], "<")
        self.assertEqual(third["项目"], "超敏C反应蛋白")
        self.assertEqual(third["单位"], "mg/L")
        self.assertEqual(fourth["性别"], "女")
        self.assertEqual(fourth["采样日期"], "2023-05-08")
        self.assertEqual([fourth[name] for name in ("年龄_岁", "年龄_月", "年龄_天")], ["6", "0", "0"])
        self.assertEqual(fourth["定性结果"], "阴性")
        self.assertEqual(fourth["结果数值"], "")
        self.assertEqual(fourth["项目"], "CRP")
        self.assertEqual(fifth["年龄"], "")
        self.assertEqual([fifth[name] for name in ("年龄_岁", "年龄_月", "年龄_天")], ["", "", ""])
        self.assertEqual(fifth["采样时间"], "")
        self.assertEqual(fifth["采样日期"], "")
        self.assertEqual(fifth["结果数值"], "1120")
        self.assertEqual(fifth["住院号"], "0045678")
        self.assertTrue(any(item["row"] == "5" and item["flag_code"] == "out_of_range" for item in flags))
        self.assertTrue(any(
            item["row"] == "5" and item["column"] == "年龄" and item["flag_code"] == "sentinel_missing"
            for item in flags
        ))
        self.assertIn({
            "row": "5", "column": "年龄", "old_value": "999",
            "new_value": "", "step_id": "S2", "op": "sentinel_to_missing",
        }, changes)
        for column, old_value, new_value, step_id in (
            ("性别", "1", "男", "S4"),
            ("项目", "HGB", "血红蛋白", "S5"),
            ("结果数值", "11.2", "112", "S9"),
            ("单位", "g/dL", "g/L", "S9"),
        ):
            self.assertTrue(any(
                item["row"] == "2" and item["column"] == column
                and item["old_value"] == old_value and item["new_value"] == new_value
                and item["step_id"] == step_id for item in changes
            ))
        self.assertEqual(list(rows[0]), [
            "住院号", "年龄", "性别", "采样时间", "项目", "结果", "单位",
            "采样日期", "年龄_岁", "年龄_月", "年龄_天",
            "结果数值", "结果限定", "定性结果", "结果无效",
        ])
        self.assertEqual(manifest["input"]["row_count"], len(rows))
        self.assertEqual(manifest["recipe"]["step_count"], 11)

    def test_additional_fixture_cases(self):
        self.apply()
        original = csv_rows(self.input)
        cleaned = csv_rows(self.target / "cleaned.csv")
        flags = csv_rows(self.target / "flags.csv")
        seen = set()
        for number, (before, after) in enumerate(zip(original, cleaned), 1):
            row_flags = [item for item in flags if item["row"] == str(number)]
            if before["结果"] == "１１０":
                self.assertEqual(after["结果"], "110")
                self.assertEqual(after["结果数值"], "110")
                seen.add("fullwidth")
            if before["结果"] == "溶血":
                self.assertEqual(after["结果无效"], "1")
                self.assertEqual(after["结果数值"], "")
                seen.add("invalid")
            if before["采样时间"] == "2023年5月":
                self.assertEqual(after["采样日期"], "")
                self.assertTrue(any(item["flag_code"] == "unparsed_date" for item in row_flags))
                seen.add("date")
            if before["性别"] == "未知":
                self.assertEqual(after["性别"], "未知")
                self.assertTrue(any(item["step_id"] == "S4" for item in row_flags))
                seen.add("sex")
            if before["项目"] == "血红蛋白" and before["单位"] == "mmol/L":
                self.assertEqual(after["单位"], "mmol/L")
                self.assertTrue(any(item["flag_code"] == "unknown_unit" for item in row_flags))
                seen.add("unit")
            if before["结果"] == "≥5":
                self.assertEqual(after["结果数值"], "5")
                self.assertEqual(after["结果限定"], ">=")
                seen.add("qualifier")
        self.assertEqual(seen, {"fullwidth", "invalid", "date", "sex", "unit", "qualifier"})

    def test_pending_step_rejects_entire_recipe_and_names_step(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][3]["approval"].update(status="pending"))
        with self.assertRaises(RecipeError) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "recipe_step_not_approved")
        self.assertIn("S4", caught.exception.message)
        self.assertFalse(self.target.exists())

    def test_rejected_step_cannot_execute(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][0]["approval"].update(status="rejected"))
        with self.assertRaises(RecipeError) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "recipe_step_not_approved")

    def test_unknown_operation_rejects_recipe(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][0].update(op="arbitrary_code"))
        with self.assertRaises((RecipeError, OperationError)) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "recipe_invalid")

    def test_invalid_parameters_reject_recipe(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][8]["params"]["conversions"][0].update(factor=10))
        with self.assertRaises((RecipeError, OperationError)) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "recipe_invalid")

    def test_input_hash_mismatch(self):
        recipe = self.recipe_copy(lambda obj: obj["input"].update(sha256="0" * 64))
        with self.assertRaises(EngineError) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "input_hash_mismatch")

    def test_missing_column_rejects_recipe(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][0]["params"].update(column="不存在的列"))
        with self.assertRaises((RecipeError, OperationError)) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "column_missing")

    def test_header_order_must_match_recipe(self):
        recipe = self.recipe_copy(lambda obj: obj["input"]["columns"].reverse())
        with self.assertRaises(EngineError) as caught:
            self.apply(recipe)
        self.assertEqual(caught.exception.code, "input_columns_mismatch")

    def test_existing_output_directory_is_never_overwritten(self):
        self.target.mkdir()
        marker = self.target / "existing.txt"
        marker.write_text("synthetic", encoding="utf-8")
        with self.assertRaises(EngineError) as caught:
            self.apply()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(marker.read_text(encoding="utf-8"), "synthetic")
        self.assertEqual(list(self.target.iterdir()), [marker])

    def test_output_outside_artifacts_is_rejected(self):
        with self.assertRaises(OutputDirectoryError) as caught:
            self.apply(target=self.root / "outside")
        self.assertEqual(caught.exception.code, "output_path_not_allowed")

    def test_paths_error_code_and_keyword_parameter(self):
        with self.assertRaises(OutputDirectoryError) as caught:
            validate_output_directory(project_root=self.root, output_directory=self.artifacts)
        self.assertEqual(caught.exception.code, "output_path_not_allowed")
        self.assertEqual(caught.exception.message, "输出目录必须是项目 artifacts/ 下的独立子目录。")

    def test_operation_failure_leaves_no_output(self):
        recipe = self.recipe_copy(lambda obj: obj["steps"][3]["params"].update(on_unmapped="error"))
        with self.assertRaises((RecipeError, OperationError)):
            self.apply(recipe)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_manifest_write_failure_cleans_staging_directory(self):
        with patch("researchops_clean.engine.write_json_atomic", side_effect=OSError("synthetic")):
            with self.assertRaises(EngineError) as caught:
                self.apply(target=self.artifacts / "new-parent" / "run")
        self.assertEqual(caught.exception.code, "output_write_failed")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_directory_rename_failure_cleans_staging_directory(self):
        with patch("researchops_clean.engine._rename_without_replacement", side_effect=OSError("synthetic")):
            with self.assertRaises(EngineError):
                self.apply()
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_concurrently_created_empty_output_is_not_replaced(self):
        rename = engine._rename_without_replacement

        def create_destination_then_rename(source, target):
            target.mkdir()
            return rename(source, target)

        with patch("researchops_clean.engine._rename_without_replacement",
                   side_effect=create_destination_then_rename):
            with self.assertRaises(EngineError) as caught:
                self.apply()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertTrue(self.target.is_dir())
        self.assertEqual(list(self.target.iterdir()), [])
        self.assertEqual(list(self.artifacts.iterdir()), [self.target])

    def test_numeric_runtime_failure_is_reported_without_publishing(self):
        with patch("researchops_clean.engine.execute_operation", side_effect=ArithmeticError("synthetic")):
            with self.assertRaises(EngineError) as caught:
                self.apply()
        self.assertEqual(caught.exception.code, "numeric_operation_failed")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_outputs_are_byte_identical_and_match_pinned_cross_platform_hash(self):
        first = self.apply()
        other = self.artifacts / "another"
        second = self.apply(target=other)
        self.assertEqual(first["run_id"], second["run_id"])
        for name in engine.OUTPUT_NAMES:
            data = (self.target / name).read_bytes()
            self.assertEqual(data, (other / name).read_bytes())
            self.assertNotIn(b"\r", data)
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
        self.assertEqual(
            hashlib.sha256((self.target / "cleaned.csv").read_bytes()).hexdigest(),
            EXPECTED_CLEANED_SHA256,
        )
        self.assertEqual({path.name for path in self.target.iterdir()}, {
            "cleaned.csv", "changes.csv", "flags.csv", "run_manifest.json",
        })

    def test_manifest_excludes_absolute_paths_and_counts_changed_and_flagged_cells(self):
        manifest = self.apply()
        text = (self.target / "run_manifest.json").read_text(encoding="utf-8")
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn(str(FIXTURES.resolve()), text)
        self.assertEqual(manifest["input"]["filename"], "lab_example.csv")
        changes = csv_rows(self.target / "changes.csv")
        flags = csv_rows(self.target / "flags.csv")
        for step in manifest["steps"]:
            self.assertEqual(step["op_version"], "1")
            self.assertEqual(step["changed_cells"], len({
                (item["row"], item["column"]) for item in changes if item["step_id"] == step["step_id"]
            }))
            self.assertEqual(step["flagged_cells"], len({
                (item["row"], item["column"]) for item in flags if item["step_id"] == step["step_id"]
            }))

    def test_run_id_does_not_depend_on_clock_or_python_version(self):
        original = self.apply()
        with patch("researchops_clean.engine.datetime") as clock:
            clock.now.return_value = datetime(2000, 1, 1, tzinfo=timezone.utc)
            with patch("researchops_clean.engine.platform.python_version", return_value="3.12.0"):
                other = self.apply(target=self.artifacts / "other-environment")
        self.assertNotEqual(original["created_at_utc"], other["created_at_utc"])
        self.assertEqual(original["run_id"], other["run_id"])

    def test_verify_success(self):
        self.apply()
        result = self.verify()
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.code, "verified")

    def test_verify_changed_output_returns_two(self):
        self.apply()
        output = self.target / "cleaned.csv"
        data = bytearray(output.read_bytes())
        data[-2] ^= 1
        output.write_bytes(data)
        self.assertEqual(self.verify().exit_code, 2)

    def test_verify_missing_output_returns_two(self):
        self.apply()
        (self.target / "flags.csv").unlink()
        self.assertEqual(self.verify().exit_code, 2)

    def test_verify_changed_recipe_returns_two(self):
        self.apply()
        recipe = self.recipe_copy(lambda obj: obj["steps"][0].update(reason="不同的合成理由"))
        result = verify_run(self.input, recipe, self.target, project_root=self.root)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.code, "recipe_hash_mismatch")

    def test_verify_changed_input_returns_two(self):
        self.apply()
        changed = self.root / "changed.csv"
        changed.write_bytes(self.input.read_bytes().replace(b"0012345", b"0099999", 1))
        result = verify_run(changed, self.recipe, self.target, project_root=self.root)
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.code, "input_hash_mismatch")

    def test_verify_different_replay_with_intact_files_returns_one(self):
        self.apply()
        execute = engine.execute_operation

        def different(op, params, columns, rows, step_id):
            result = execute(op, params, columns, rows, step_id)
            if step_id == "S11":
                rows[0]["住院号"] = "synthetic-replay-change"
            return result

        with patch("researchops_clean.engine.execute_operation", side_effect=different):
            self.assertEqual(self.verify().exit_code, 1)

    def test_verify_changed_manifest_identity_returns_two(self):
        self.apply()
        path = self.target / "run_manifest.json"
        manifest = json.loads(path.read_text(encoding="utf-8"))
        manifest["steps"][0]["changed_cells"] += 1
        path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(self.verify().exit_code, 2)

    def test_verify_deeply_corrupted_manifest_returns_two(self):
        self.target.mkdir()
        (self.target / "run_manifest.json").write_text(
            "[" * 2000 + "]" * 2000, encoding="utf-8"
        )
        result = self.verify()
        self.assertEqual(result.exit_code, 2)
        self.assertEqual(result.code, "run_manifest_invalid")

    def test_verify_replays_gbk_using_recorded_encoding(self):
        data = self.input.read_text(encoding="utf-8").encode("gbk")
        source = self.root / "gbk.csv"
        source.write_bytes(data)
        recipe = self.recipe_copy(lambda obj: obj["input"].update(sha256=hashlib.sha256(data).hexdigest()))
        manifest = self.apply(recipe, input_path=source, encoding="gbk")
        self.assertEqual(manifest["input"]["encoding"], "gbk")
        self.assertEqual(
            verify_run(source, recipe, self.target, project_root=self.root).exit_code, 0
        )

    def test_formula_like_values_are_only_reported(self):
        rows = csv_rows(self.input)
        rows[0]["结果"] = "=SUM(A1)"
        rows[1]["结果"] = "-12.5"
        columns = list(rows[0])
        stream = io.StringIO(newline="")
        writer = csv.DictWriter(stream, fieldnames=columns, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)
        data = stream.getvalue().encode("utf-8")
        source = self.root / "formula.csv"
        source.write_bytes(data)
        recipe = self.recipe_copy(lambda obj: obj["input"].update(sha256=hashlib.sha256(data).hexdigest()))
        manifest = self.apply(recipe, input_path=source)
        cleaned = csv_rows(self.target / "cleaned.csv")
        self.assertEqual(cleaned[0]["结果"], "=SUM(A1)")
        self.assertEqual(cleaned[1]["结果"], "-12.5")
        expected = sum(
            value.startswith(("=", "+", "-", "@")) and not engine.is_number(value)
            for row in cleaned for value in row.values()
        )
        self.assertGreaterEqual(expected, 1)
        self.assertEqual(manifest["warnings"]["formula_like_cells"], expected)
