from __future__ import annotations

import copy
import hashlib
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean.bench.generate import generate_benchmark
from researchops_clean.csv_io import csv_bytes, read_csv
from researchops_clean.engine import apply_recipe, verify_run
from researchops_clean.recipe import approve_recipe, step_content_hash


def exact_hash(content):
    return hashlib.sha256(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


class ApprovalOrderTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input_path = self.root / "synthetic.csv"
        self.input_path.write_bytes(csv_bytes(
            ["性别", "备注"],
            [{"性别": "男", "备注": "  合成  "}, {"性别": "女", "备注": "示例"}],
        ))
        self.pending = {
            "schema_version": "cleaning-recipe/1",
            "input": {"sha256": read_csv(self.input_path).sha256, "columns": ["性别", "备注"]},
            "steps": [
                {
                    "step_id": "S1", "op": "map_values",
                    "params": {
                        "column": "性别", "mapping": {"男": "男", "女": "女"},
                        "on_unmapped": "keep_and_flag",
                    },
                    "reason": "合成映射", "approval": {"status": "pending"},
                },
                {
                    "step_id": "S2", "op": "normalize_text",
                    "params": {"column": "备注"},
                    "reason": "合成文本规范化", "approval": {"status": "pending"},
                },
            ],
        }
        self.approved = self.approve(self.pending)
        self.counter = 0

    def approve(self, recipe, step_ids=None):
        return approve_recipe(
            recipe,
            [step["step_id"] for step in recipe["steps"]] if step_ids is None else step_ids,
            "reviewer-test", decided_at_utc="2026-01-01T00:00:00Z",
        )

    def save_recipe(self, recipe, name):
        path = self.root / name
        path.write_bytes((json.dumps(recipe, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        return path

    def cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(Path, "cwd", return_value=self.root), redirect_stdout(stdout), redirect_stderr(stderr):
            exit_code = main(arguments)
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        return exit_code, json.loads(stdout.getvalue() or stderr.getvalue())

    def assert_rejected(
        self, recipe, expected_ids, *, original_recipe=None, input_path=None,
        existing_run=None,
    ):
        self.counter += 1
        source_input = self.input_path if input_path is None else input_path
        source_recipe = self.approved if original_recipe is None else original_recipe
        if existing_run is None:
            source_path = self.save_recipe(source_recipe, f"original-{self.counter}.json")
            existing_run = self.root / "artifacts" / f"original-{self.counter}"
            apply_recipe(source_input, source_path, existing_run, project_root=self.root)
        changed_path = self.save_recipe(recipe, f"changed-{self.counter}.json")
        output = self.root / "artifacts" / f"rejected-{self.counter}"
        with patch("researchops_clean.engine._execute") as execute:
            code, report = self.cli([
                "apply", "--input", str(source_input), "--recipe", str(changed_path),
                "--output-dir", str(output),
            ])
            self.assertEqual(code, 2)
            self.assertEqual(report["error_code"], "approval_content_mismatch")
            self.assertEqual(re.findall(r"\b(?:S|B)[0-9]+\b", report["message"]), expected_ids)
            self.assertNotIn(str(self.root), report["message"])
            code, report = self.cli([
                "verify", "--input", str(source_input), "--recipe", str(changed_path),
                "--run-dir", str(existing_run),
            ])
            self.assertEqual(code, 2)
            self.assertEqual(report["code"], "approval_content_mismatch")
            self.assertEqual(re.findall(r"\b(?:S|B)[0-9]+\b", report["message"]), expected_ids)
            self.assertNotIn(str(self.root), report["message"])
            execute.assert_not_called()
        self.assertFalse(output.exists())

    def assert_executes(self, recipe):
        self.counter += 1
        path = self.save_recipe(recipe, f"ready-{self.counter}.json")
        output = self.root / "artifacts" / f"ready-{self.counter}"
        apply_recipe(self.input_path, path, output, project_root=self.root)
        result = verify_run(self.input_path, path, output, project_root=self.root)
        self.assertEqual(result.exit_code, 0, result.message)
        return read_csv(output / "cleaned.csv")

    def test_hash_contains_the_complete_order_including_pending_steps(self):
        partial = self.approve(self.pending, ["S1"])
        self.assertEqual(partial["steps"][1]["approval"], {"status": "pending"})
        first = partial["steps"][0]
        expected = exact_hash({
            "input_sha256": partial["input"]["sha256"],
            "step_id": first["step_id"], "op": first["op"], "params": first["params"],
            "step_order": ["S1", "S2"],
        })
        self.assertEqual(first["approval"]["content_sha256"], expected)
        self.assertEqual(step_content_hash(partial, first), expected)
        reversed_recipe = copy.deepcopy(partial)
        reversed_recipe["steps"].reverse()
        self.assertNotEqual(step_content_hash(reversed_recipe, reversed_recipe["steps"][1]), expected)
        shortened_recipe = copy.deepcopy(partial)
        shortened_recipe["steps"].pop()
        self.assertNotEqual(step_content_hash(shortened_recipe, shortened_recipe["steps"][0]), expected)

    def test_swapping_approved_steps_invalidates_every_remaining_approval(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"].reverse()
        self.assert_rejected(changed, ["S2", "S1"])

    def test_deleting_a_step_invalidates_the_remaining_approval(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"].pop()
        self.assert_rejected(changed, ["S1"])

    def test_approving_only_an_added_step_leaves_both_old_approvals_stale(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"].append({
            "step_id": "S3", "op": "normalize_text", "params": {"column": "性别"},
            "reason": "新增合成步骤", "approval": {"status": "pending"},
        })
        changed = self.approve(changed, ["S3"])
        self.assertEqual(
            changed["steps"][2]["approval"]["content_sha256"],
            step_content_hash(changed, changed["steps"][2]),
        )
        self.assert_rejected(changed, ["S1", "S2"])

    def test_renaming_and_reapproving_only_that_step_leaves_the_other_stale(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"][0]["step_id"] = "S9"
        changed = self.approve(changed, ["S9"])
        self.assertEqual(
            changed["steps"][0]["approval"]["content_sha256"],
            step_content_hash(changed, changed["steps"][0]),
        )
        self.assert_rejected(changed, ["S2"])

    def test_parameter_change_only_invalidates_its_own_approval(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"][0]["params"]["mapping"]["男"] = "男性"
        self.assert_rejected(changed, ["S1"])

    def test_operation_change_only_invalidates_its_own_approval(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"][0]["op"] = "normalize_text"
        changed["steps"][0]["params"] = {"column": "性别"}
        self.assert_rejected(changed, ["S1"])

    def test_reason_only_edits_still_execute_and_verify(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"][0]["reason"] = "补充原因说明"
        changed["steps"][1]["reason"] = "再次补充原因说明"
        for original, edited in zip(self.approved["steps"], changed["steps"]):
            self.assertEqual(
                original["approval"]["content_sha256"], step_content_hash(changed, edited),
            )
        table = self.assert_executes(changed)
        self.assertEqual(table.rows[0]["备注"], "合成")

    def test_reapproving_all_steps_in_the_new_order_executes_and_verifies(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"].reverse()
        rebound = self.approve(changed)
        for step in rebound["steps"]:
            self.assertEqual(step["approval"]["content_sha256"], step_content_hash(rebound, step))
        table = self.assert_executes(rebound)
        self.assertEqual(table.rows[0]["备注"], "合成")

    def test_two_separate_approvals_with_unchanged_order_execute_and_verify(self):
        partial = self.approve(self.pending, ["S1"])
        first_hash = partial["steps"][0]["approval"]["content_sha256"]
        complete = self.approve(partial, ["S2"])
        self.assertEqual(complete["steps"][0]["approval"]["content_sha256"], first_hash)
        self.assert_executes(complete)

    def test_deleting_then_reapproving_remaining_steps_executes_and_verifies(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"].pop()
        rebound = self.approve(changed)
        self.assertNotEqual(
            rebound["steps"][0]["approval"]["content_sha256"],
            self.approved["steps"][0]["approval"]["content_sha256"],
        )
        table = self.assert_executes(rebound)
        self.assertEqual(table.rows[0]["备注"], "  合成  ")

    def test_legacy_four_field_approval_hashes_are_all_rejected(self):
        legacy = copy.deepcopy(self.approved)
        for step in legacy["steps"]:
            step["approval"]["content_sha256"] = exact_hash({
                "input_sha256": legacy["input"]["sha256"],
                "step_id": step["step_id"], "op": step["op"], "params": step["params"],
            })
            self.assertNotEqual(
                step["approval"]["content_sha256"], step_content_hash(legacy, step),
            )
        self.assert_rejected(legacy, ["S1", "S2"])

    def test_benchmark_order_swap_and_deletion_are_rejected_before_execution(self):
        bench = self.root / "artifacts" / "bench"
        generate_benchmark(rows=200, seed=7, output_dir=bench, project_root=self.root)
        recipe_path = bench / "reference_recipe.json"
        reference = json.loads(recipe_path.read_text(encoding="utf-8"))
        ids = [step["step_id"] for step in reference["steps"]]
        self.assertTrue({"B5", "B6", "B12"}.issubset(ids))
        dirty = bench / "dirty.csv"
        run = self.root / "artifacts" / "bench-run"
        apply_recipe(dirty, recipe_path, run, project_root=self.root)
        self.assertEqual(verify_run(dirty, recipe_path, run, project_root=self.root).exit_code, 0)
        swapped = copy.deepcopy(reference)
        left, right = ids.index("B5"), ids.index("B6")
        swapped["steps"][left], swapped["steps"][right] = swapped["steps"][right], swapped["steps"][left]
        self.assert_rejected(
            swapped, [step["step_id"] for step in swapped["steps"]],
            original_recipe=reference, input_path=dirty, existing_run=run,
        )
        shortened = copy.deepcopy(reference)
        shortened["steps"] = [step for step in shortened["steps"] if step["step_id"] != "B12"]
        self.assert_rejected(
            shortened, [step["step_id"] for step in shortened["steps"]],
            original_recipe=reference, input_path=dirty, existing_run=run,
        )


if __name__ == "__main__":
    unittest.main()
