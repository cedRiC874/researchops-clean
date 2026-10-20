"""Contracts for the two permitted changes to existing cleaning primitives."""
from __future__ import annotations

import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean.ops import execute_operation, validate_operation
from researchops_clean.sensitive_patterns import find_sensitive_types


class SensitiveTypesTests(unittest.TestCase):
    def test_each_existing_sensitive_category_is_exposed(self):
        for value, expected in (
            ("11010519491231002X", ["identity_number"]),
            ("13800000000", ["phone_number"]),
            ("synthetic@example.test", ["email"]),
            ("C:\\synthetic\\data.csv", ["file_path"]),
            ("sk-synthetic-only", ["api_key"]),
        ):
            with self.subTest(expected=expected):
                self.assertEqual(find_sensitive_types(value), expected)

    def test_fixed_order_is_independent_of_occurrence_order(self):
        text = (
            "sk-synthetic-only C:\\synthetic\\data.csv synthetic@example.test "
            "13800000000 11010519491231002X"
        )
        self.assertEqual(find_sensitive_types(text), [
            "identity_number", "phone_number", "email", "file_path", "api_key",
        ])

    def test_multiple_occurrences_do_not_duplicate_categories(self):
        self.assertEqual(
            find_sensitive_types("13800000000 13900000000 synthetic@example.test"),
            ["phone_number", "email"],
        )

    def test_ordinary_values_and_units_remain_unmatched(self):
        for text in ("", "男", "阴性", "<0.5", "g/L", "2023/5/6 8:30", "3岁2月"):
            with self.subTest(text=text):
                self.assertEqual(find_sensitive_types(text), [])


class MappingApprovalContracts(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()
        self.input = self.root / "data.csv"
        self.input.write_text("性别\nM\n男\n", encoding="utf-8", newline="\n")
        self.recipe_path = self.root / "recipe.json"

    def call(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([str(argument) for argument in arguments])
        self.assertNotIn(str(self.root), stdout.getvalue() + stderr.getvalue())
        return code, stdout.getvalue(), stderr.getvalue()

    def write_recipe(self, mapping, *, approved=False):
        approval = (
            {"status": "approved", "reviewer": "reviewer-test",
             "decided_at_utc": "2026-10-20T08:00:00Z"}
            if approved else {"status": "pending"}
        )
        recipe = {
            "schema_version": "cleaning-recipe/1",
            "input": {
                "sha256": hashlib.sha256(self.input.read_bytes()).hexdigest(),
                "columns": ["性别"],
            },
            "steps": [{
                "step_id": "S1",
                "op": "map_values",
                "params": {
                    "column": "性别",
                    "mapping": mapping,
                    "on_unmapped": "keep_and_flag",
                },
                "reason": "手写合成映射",
                "approval": approval,
            }],
        }
        self.recipe_path.write_text(
            json.dumps(recipe, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )

    def assert_approve_rejects(self, mapping):
        self.write_recipe(mapping)
        output = self.root / "approved.json"
        original = self.recipe_path.read_bytes()
        code, stdout, stderr = self.call(
            "approve", "--recipe", self.recipe_path, "--steps", "S1",
            "--reviewer", "reviewer-test", "--output", output,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "recipe_invalid")
        self.assertFalse(output.exists())
        self.assertEqual(self.recipe_path.read_bytes(), original)

    def assert_apply_rejects(self, mapping):
        self.write_recipe(mapping, approved=True)
        output = self.root / "artifacts" / "rejected"
        code, stdout, stderr = self.call(
            "apply", "--input", self.input, "--recipe", self.recipe_path,
            "--output-dir", output,
        )
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "recipe_invalid")
        self.assertFalse(output.exists())

    def test_approve_rejects_empty_mapping(self):
        self.assert_approve_rejects({})

    def test_approve_rejects_any_empty_target(self):
        self.assert_approve_rejects({"M": "男", "男": ""})

    def test_apply_rejects_empty_mapping_even_if_marked_approved(self):
        self.assert_apply_rejects({})

    def test_apply_rejects_any_empty_target_even_if_marked_approved(self):
        self.assert_apply_rejects({"M": "男", "男": ""})

    def test_blank_source_key_and_nonempty_whitespace_target_stay_allowed(self):
        params = {
            "column": "性别",
            "mapping": {"": "不能填补", "M": " "},
            "on_unmapped": "keep_and_flag",
        }
        self.assertEqual(validate_operation("map_values", params, ["性别"]), [])
        rows = [{"性别": ""}, {"性别": "M"}, {"性别": "男"}]
        _, flags = execute_operation("map_values", params, ["性别"], rows, "S1")
        self.assertEqual(rows, [{"性别": ""}, {"性别": " "}, {"性别": "男"}])
        self.assertEqual([flag["row"] for flag in flags], [3])
        self.assertEqual([flag["flag_code"] for flag in flags], ["unmapped_value"])

    def test_completed_identity_mapping_can_be_approved_and_applied(self):
        self.write_recipe({"M": "男", "男": "男"})
        approved = self.root / "approved.json"
        code, _, stderr = self.call(
            "approve", "--recipe", self.recipe_path, "--steps", "S1",
            "--reviewer", "reviewer-test", "--output", approved,
        )
        self.assertEqual(code, 0, stderr)
        output = self.root / "artifacts" / "complete"
        code, _, stderr = self.call(
            "apply", "--input", self.input, "--recipe", approved, "--output-dir", output,
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual((output / "cleaned.csv").read_text(encoding="utf-8"), "性别\n男\n男\n")
        self.assertEqual(
            len((output / "flags.csv").read_text(encoding="utf-8").splitlines()), 1
        )


if __name__ == "__main__":
    unittest.main()
