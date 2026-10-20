from __future__ import annotations

import copy
import hashlib
import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean.csv_io import csv_bytes, read_csv
from researchops_clean.engine import EngineError, apply_recipe, verify_run
from researchops_clean.ledger import sha256_json
from researchops_clean.recipe import (
    RecipeError,
    approve_recipe,
    step_content_hash,
    validate_approval_content,
    validate_recipe,
    validate_steps,
)


class ApprovalBindingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.input_path = self.root / "synthetic.csv"
        self.input_path.write_bytes(csv_bytes(["性别"], [{"性别": "男"}, {"性别": "女"}]))
        self.pending = {
            "schema_version": "cleaning-recipe/1",
            "input": {"sha256": read_csv(self.input_path).sha256, "columns": ["性别"]},
            "steps": [
                {
                    "step_id": "S1",
                    "op": "map_values",
                    "params": {
                        "column": "性别", "mapping": {"男": "男", "女": "女"},
                        "on_unmapped": "keep_and_flag",
                    },
                    "reason": "合成数据映射",
                    "approval": {"status": "pending"},
                },
                {
                    "step_id": "S2", "op": "normalize_text",
                    "params": {"column": "性别"},
                    "reason": "合成文本规范化",
                    "approval": {"status": "pending"},
                },
            ],
        }
        self.approved = approve_recipe(
            self.pending, ["S1", "S2"], "reviewer-test",
            decided_at_utc="2026-01-01T00:00:00Z",
        )
        self.counter = 0

    def save_recipe(self, recipe, name):
        path = self.root / name
        path.write_bytes((json.dumps(recipe, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8"))
        return path

    def cli(self, arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(Path, "cwd", return_value=self.root), redirect_stdout(stdout), redirect_stderr(stderr):
            code = main(arguments)
        self.assertNotIn("Traceback", stdout.getvalue() + stderr.getvalue())
        return code, json.loads(stdout.getvalue() or stderr.getvalue())

    def assert_binding_rejected(self, recipe, *, step_ids=("S1",), input_path=None):
        self.counter += 1
        approved_path = self.save_recipe(self.approved, f"original-{self.counter}.json")
        original_run = self.root / "artifacts" / f"original-{self.counter}"
        apply_recipe(self.input_path, approved_path, original_run, project_root=self.root)
        tampered_path = self.save_recipe(recipe, f"tampered-{self.counter}.json")
        rejected_output = self.root / "artifacts" / f"rejected-{self.counter}"
        with patch("researchops_clean.engine._execute") as execute:
            with self.assertRaises((RecipeError, EngineError)) as failure:
                apply_recipe(
                    input_path or self.input_path, tampered_path, rejected_output,
                    project_root=self.root,
                )
            self.assertEqual(failure.exception.code, "approval_content_mismatch")
            for step_id in step_ids:
                self.assertIn(step_id, failure.exception.message)
            verified = verify_run(
                input_path or self.input_path, tampered_path, original_run,
                project_root=self.root,
            )
            self.assertEqual(verified.exit_code, 2)
            self.assertEqual(verified.code, "approval_content_mismatch")
            for step_id in step_ids:
                self.assertIn(step_id, verified.message)
            execute.assert_not_called()
        self.assertFalse(rejected_output.exists())
        return tampered_path, original_run

    def test_hash_binds_exact_input_step_operation_and_parameters(self):
        for step in self.pending["steps"]:
            content = {
                "input_sha256": self.pending["input"]["sha256"],
                "step_order": [item["step_id"] for item in self.pending["steps"]],
                "step_id": step["step_id"],
                "op": step["op"],
                "params": step["params"],
            }
            expected = hashlib.sha256(json.dumps(
                content, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
            ).encode("utf-8")).hexdigest()
            self.assertEqual(step_content_hash(self.pending, step), expected)
            self.assertEqual(len(expected), 64)
        original = self.pending["steps"][0]
        original_hash = step_content_hash(self.pending, original)
        for field, value in (("step_id", "S9"), ("op", "normalize_text"), ("params", {"column": "性别"})):
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                self.assertNotEqual(step_content_hash(self.pending, changed), original_hash)
        other_input = copy.deepcopy(self.pending)
        other_input["input"]["sha256"] = "0" * 64
        self.assertNotEqual(step_content_hash(other_input, original), original_hash)

    def test_approve_stores_content_hash_and_preserves_pending_source(self):
        for step in self.approved["steps"]:
            self.assertEqual(step["approval"]["status"], "approved")
            self.assertEqual(step["approval"]["content_sha256"], step_content_hash(self.approved, step))
        for step in self.pending["steps"]:
            self.assertEqual(step["approval"], {"status": "pending"})
        validate_approval_content(self.approved)

    def test_reason_and_approval_metadata_do_not_change_bound_hash(self):
        original = self.approved["steps"][0]
        changed = copy.deepcopy(original)
        changed["reason"] = "仅补充原因说明"
        changed["approval"]["reviewer"] = "another-reviewer"
        changed["approval"]["decided_at_utc"] = "2026-02-01T00:00:00Z"
        self.assertEqual(step_content_hash(self.approved, changed), step_content_hash(self.approved, original))

    def test_parameter_tampering_rejects_apply_and_verify_before_execution(self):
        tampered = copy.deepcopy(self.approved)
        tampered["steps"][0]["params"]["mapping"]["男"] = "男性"
        self.assert_binding_rejected(tampered)

    def test_nfc_equivalent_mapping_keys_and_values_have_distinct_approval_hashes(self):
        for location in ("mapping_value", "mapping_key"):
            with self.subTest(location=location):
                pending = copy.deepcopy(self.pending)
                mapping = pending["steps"][0]["params"]["mapping"]
                if location == "mapping_value":
                    mapping["男"] = "\u00e9"
                else:
                    mapping["\u00e9"] = "规范值"
                self.approved = approve_recipe(
                    pending, ["S1", "S2"], "reviewer-test",
                    decided_at_utc="2026-01-01T00:00:00Z",
                )
                tampered = copy.deepcopy(self.approved)
                mapping = tampered["steps"][0]["params"]["mapping"]
                if location == "mapping_value":
                    mapping["男"] = "e\u0301"
                else:
                    mapping["e\u0301"] = mapping.pop("\u00e9")

                def content(recipe):
                    step = recipe["steps"][0]
                    return {
                        "input_sha256": recipe["input"]["sha256"],
                        "step_order": [item["step_id"] for item in recipe["steps"]],
                        "step_id": step["step_id"], "op": step["op"], "params": step["params"],
                    }

                # The ledger keeps its existing normalization; approval binding
                # must distinguish the exact parameter text nevertheless.
                self.assertEqual(sha256_json(content(self.approved)), sha256_json(content(tampered)))
                for recipe in (self.approved, tampered):
                    expected = hashlib.sha256(json.dumps(
                        content(recipe), ensure_ascii=False, sort_keys=True,
                        separators=(",", ":"), allow_nan=False,
                    ).encode("utf-8")).hexdigest()
                    self.assertEqual(step_content_hash(recipe, recipe["steps"][0]), expected)
                self.assertNotEqual(
                    step_content_hash(self.approved, self.approved["steps"][0]),
                    step_content_hash(tampered, tampered["steps"][0]),
                )
                self.assert_binding_rejected(tampered)

    def test_operation_and_step_identity_tampering_are_rejected(self):
        operation_changed = copy.deepcopy(self.approved)
        operation_changed["steps"][0]["op"] = "normalize_text"
        operation_changed["steps"][0]["params"] = {"column": "性别"}
        self.assert_binding_rejected(operation_changed)
        step_changed = copy.deepcopy(self.approved)
        step_changed["steps"][0]["step_id"] = "S9"
        self.assert_binding_rejected(step_changed, step_ids=("S9",))

    def test_approved_step_cannot_be_moved_to_another_input(self):
        alternate = self.root / "alternate.csv"
        alternate.write_bytes(csv_bytes(["性别"], [{"性别": "女"}, {"性别": "男"}]))
        moved = copy.deepcopy(self.approved)
        moved["input"]["sha256"] = read_csv(alternate).sha256
        self.assertNotEqual(moved["input"]["sha256"], self.approved["input"]["sha256"])
        self.assert_binding_rejected(moved, step_ids=("S1", "S2"), input_path=alternate)

    def test_missing_and_mismatched_hashes_report_every_affected_step(self):
        stale = copy.deepcopy(self.approved)
        del stale["steps"][0]["approval"]["content_sha256"]
        stale["steps"][1]["approval"]["content_sha256"] = "0" * 64
        self.assert_binding_rejected(stale, step_ids=("S1", "S2"))

    def test_reason_only_edit_can_create_and_verify_a_new_run(self):
        changed = copy.deepcopy(self.approved)
        changed["steps"][0]["reason"] = "补充合成映射的理由"
        self.assertEqual(
            changed["steps"][0]["approval"]["content_sha256"],
            step_content_hash(changed, changed["steps"][0]),
        )
        path = self.save_recipe(changed, "reason-edited.json")
        output = self.root / "artifacts" / "reason-edited"
        apply_recipe(self.input_path, path, output, project_root=self.root)
        result = verify_run(self.input_path, path, output, project_root=self.root)
        self.assertEqual(result.exit_code, 0, result.message)

    def test_reapproval_rebinds_modified_parameters_and_enables_execution(self):
        changed = copy.deepcopy(self.approved)
        old_hash = changed["steps"][0]["approval"]["content_sha256"]
        changed["steps"][0]["params"]["mapping"]["男"] = "男性"
        rebound = approve_recipe(
            changed, ["S1"], "reviewer-test", decided_at_utc="2026-02-01T00:00:00Z",
        )
        self.assertNotEqual(rebound["steps"][0]["approval"]["content_sha256"], old_hash)
        self.assertEqual(
            rebound["steps"][0]["approval"]["content_sha256"],
            step_content_hash(rebound, rebound["steps"][0]),
        )
        path = self.save_recipe(rebound, "reapproved.json")
        output = self.root / "artifacts" / "reapproved"
        apply_recipe(self.input_path, path, output, project_root=self.root)
        self.assertEqual(read_csv(output / "cleaned.csv").rows[0]["性别"], "男性")
        result = verify_run(self.input_path, path, output, project_root=self.root)
        self.assertEqual(result.exit_code, 0, result.message)

    def test_pending_steps_need_no_hash_and_partial_approval_only_binds_selected_steps(self):
        validate_recipe(self.pending)
        validate_approval_content(self.pending)
        validate_steps(self.pending, require_approved=False)
        partial = approve_recipe(
            self.pending, ["S1"], "reviewer-test", decided_at_utc="2026-01-01T00:00:00Z",
        )
        self.assertIn("content_sha256", partial["steps"][0]["approval"])
        self.assertEqual(partial["steps"][1]["approval"], {"status": "pending"})
        validate_approval_content(partial)
        rejected_output = self.root / "artifacts" / "pending"
        path = self.save_recipe(partial, "partially-approved.json")
        with patch("researchops_clean.engine._execute") as execute:
            with self.assertRaises((RecipeError, EngineError)):
                apply_recipe(self.input_path, path, rejected_output, project_root=self.root)
            execute.assert_not_called()
        self.assertFalse(rejected_output.exists())

    def test_approve_still_rejects_invalid_parameters(self):
        invalid = copy.deepcopy(self.pending)
        invalid["steps"][0]["params"]["mapping"] = {}
        with self.assertRaises(RecipeError):
            approve_recipe(invalid, ["S1"], "reviewer-test", decided_at_utc="2026-01-01T00:00:00Z")
        self.assertEqual(invalid["steps"][0]["approval"], {"status": "pending"})
        source = self.save_recipe(invalid, "invalid-parameters.json")
        target = self.root / "invalid-approved.json"
        code, result = self.cli([
            "approve", "--recipe", str(source), "--steps", "S1",
            "--reviewer", "reviewer-test", "--output", str(target),
        ])
        self.assertEqual(code, 2)
        self.assertTrue(result["error_code"])
        self.assertFalse(target.exists())

    def test_approve_cli_writes_hash_and_refuses_existing_output(self):
        source = self.save_recipe(self.pending, "draft.json")
        target = self.root / "approved.json"
        arguments = [
            "approve", "--recipe", str(source), "--steps", "S1,S2",
            "--reviewer", "reviewer-test", "--output", str(target),
        ]
        code, _ = self.cli(arguments)
        self.assertEqual(code, 0)
        written = json.loads(target.read_text(encoding="utf-8"))
        for step in written["steps"]:
            self.assertEqual(step["approval"]["content_sha256"], step_content_hash(written, step))
        original = target.read_bytes()
        code, result = self.cli(arguments)
        self.assertEqual(code, 2)
        self.assertTrue(result["error_code"])
        self.assertEqual(target.read_bytes(), original)

    def test_apply_and_verify_cli_return_two_for_unbound_approved_steps(self):
        stale = copy.deepcopy(self.approved)
        for step in stale["steps"]:
            del step["approval"]["content_sha256"]
        source, existing_run = self.assert_binding_rejected(stale, step_ids=("S1", "S2"))
        output = self.root / "artifacts" / "cli-rejected"
        with patch("researchops_clean.engine._execute") as execute:
            code, result = self.cli([
                "apply", "--input", str(self.input_path), "--recipe", str(source),
                "--output-dir", str(output),
            ])
            self.assertEqual(code, 2)
            self.assertEqual(result["error_code"], "approval_content_mismatch")
            for step_id in ("S1", "S2"):
                self.assertIn(step_id, result["message"])
            code, result = self.cli([
                "verify", "--input", str(self.input_path), "--recipe", str(source),
                "--run-dir", str(existing_run),
            ])
            self.assertEqual(code, 2)
            self.assertEqual(result["code"], "approval_content_mismatch")
            for step_id in ("S1", "S2"):
                self.assertIn(step_id, result["message"])
            execute.assert_not_called()
        self.assertFalse(output.exists())


if __name__ == "__main__":
    unittest.main()
