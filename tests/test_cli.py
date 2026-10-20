"""Command-line contracts using only hand-written synthetic fixtures."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean import engine
from researchops_clean.__main__ import main
from researchops_clean.recipe import recipe_hash


FIXTURES = Path(__file__).parent / "fixtures"


class CLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()
        self.input = self.root / "data.csv"
        self.recipe = self.root / "recipe.json"
        self.input.write_bytes((FIXTURES / "lab_example.csv").read_bytes())
        self.recipe.write_bytes((FIXTURES / "lab_recipe.json").read_bytes())
        self.target = self.root / "artifacts" / "run-01"

    def call(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([str(argument) for argument in arguments])
        for output in (stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn(str(self.root), output)
        return result, stdout.getvalue(), stderr.getvalue()

    def write_recipe(self, recipe):
        self.recipe.write_text(json.dumps(recipe, ensure_ascii=False) + "\n",
                               encoding="utf-8", newline="\n")

    def approve(self, *extra):
        return self.call("approve", "--recipe", self.recipe,
                         "--steps", "S1", "--reviewer", "reviewer-cli",
                         "--output", self.root / "approved.json", *extra)

    def apply(self, *extra):
        return self.call("apply", "--input", self.input, "--recipe", self.recipe,
                         "--output-dir", self.target, *extra)

    def verify(self):
        return self.call("verify", "--input", self.input, "--recipe", self.recipe,
                         "--run-dir", self.target)

    def test_ops_exposes_all_versioned_parameter_schemas(self):
        code, stdout, stderr = self.call("ops")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        catalog = json.loads(stdout)
        self.assertEqual({item["op"] for item in catalog}, {
            "normalize_text", "sentinel_to_missing", "map_values", "parse_date",
            "parse_age_cn", "parse_lab_result", "convert_unit", "range_check",
            "flag_duplicates",
        })
        for item in catalog:
            self.assertEqual(item["op_version"], "1")
            self.assertEqual(item["params_schema"]["type"], "object")

    def test_approve_changes_only_selected_steps_and_prints_hash(self):
        recipe = json.loads(self.recipe.read_text(encoding="utf-8"))
        recipe["steps"][0]["approval"] = {"status": "pending"}
        recipe["steps"][1]["approval"] = {"status": "pending"}
        recipe["steps"][1]["params"] = {}
        self.write_recipe(recipe)
        original = self.recipe.read_bytes()
        code, stdout, stderr = self.approve()
        self.assertEqual(code, 0, stderr)
        approved_path = self.root / "approved.json"
        content = approved_path.read_bytes()
        self.assertFalse(content.startswith(b"\xef\xbb\xbf"))
        self.assertNotIn(b"\r", content)
        approved = json.loads(content)
        self.assertEqual(approved["steps"][0]["approval"]["status"], "approved")
        self.assertEqual(approved["steps"][0]["approval"]["reviewer"], "reviewer-cli")
        self.assertRegex(approved["steps"][0]["approval"]["decided_at_utc"],
                         r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertEqual(approved["steps"][1], recipe["steps"][1])
        self.assertEqual(json.loads(stdout)["recipe_sha256"], recipe_hash(approved))
        self.assertEqual(self.recipe.read_bytes(), original)

    def test_approve_never_overwrites_an_existing_target(self):
        target = self.root / "approved.json"
        target.write_bytes(b"preserve this file\n")
        code, stdout, stderr = self.approve()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "output_exists")
        self.assertEqual(target.read_bytes(), b"preserve this file\n")

    def test_approve_rejects_invalid_selected_parameters(self):
        recipe = json.loads(self.recipe.read_text(encoding="utf-8"))
        recipe["steps"][0]["params"] = {}
        self.write_recipe(recipe)
        code, _, stderr = self.approve()
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(stderr)["error_code"])
        self.assertFalse((self.root / "approved.json").exists())

    def test_approve_rejects_unknown_step_ids(self):
        code, _, stderr = self.approve("--steps", "does-not-exist")
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(stderr)["error_code"])
        self.assertFalse((self.root / "approved.json").exists())

    def test_approve_rejects_empty_step_selection(self):
        code, _, stderr = self.approve("--steps", "S1,")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error_code"], "recipe_invalid")
        self.assertFalse((self.root / "approved.json").exists())

    def test_approve_rejects_path_like_reviewer(self):
        code, _, stderr = self.approve("--reviewer", "../reviewer")
        self.assertEqual(code, 2)
        self.assertTrue(json.loads(stderr)["error_code"])
        self.assertFalse((self.root / "approved.json").exists())

    def test_apply_and_verify_publish_and_replay_successfully(self):
        code, stdout, stderr = self.apply()
        self.assertEqual(code, 0, stderr)
        manifest = json.loads((self.target / "run_manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(json.loads(stdout)["run_id"], manifest["run_id"])
        self.assertEqual({path.name for path in self.target.iterdir()},
                         {"cleaned.csv", "changes.csv", "flags.csv", "run_manifest.json"})
        code, stdout, stderr = self.verify()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "verified")

    def test_apply_uses_current_directory_as_project_root(self):
        code, _, stderr = self.apply("--output-dir", "artifacts/relative-run")
        self.assertEqual(code, 0, stderr)
        self.assertTrue((self.root / "artifacts" / "relative-run" / "cleaned.csv").is_file())

    def test_apply_refuses_unapproved_recipe_with_step_id(self):
        recipe = json.loads(self.recipe.read_text(encoding="utf-8"))
        recipe["steps"][0]["approval"] = {"status": "pending"}
        self.write_recipe(recipe)
        code, stdout, stderr = self.apply()
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["error_code"], "recipe_step_not_approved")
        self.assertIn("S1", error["message"])
        self.assertFalse(self.target.exists())

    def test_apply_refuses_existing_output_without_modification(self):
        self.target.mkdir()
        sentinel = self.target / "preserve.txt"
        sentinel.write_bytes(b"unchanged")
        code, _, stderr = self.apply()
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error_code"], "output_exists")
        self.assertEqual(sentinel.read_bytes(), b"unchanged")

    def test_apply_reports_output_path_error_code(self):
        code, _, stderr = self.apply("--output-dir", self.root / "outside")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error_code"], "output_path_not_allowed")
        self.assertFalse((self.root / "outside").exists())

    def test_apply_refuses_unsupported_encoding(self):
        code, _, stderr = self.apply("--encoding", "latin-1")
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(stderr)["error_code"], "cli_invalid_arguments")
        self.assertFalse(self.target.exists())

    def test_apply_and_verify_support_gbk_input(self):
        self.input.write_bytes(self.input.read_text(encoding="utf-8").encode("gbk"))
        import hashlib
        recipe = json.loads(self.recipe.read_text(encoding="utf-8"))
        recipe["input"]["sha256"] = hashlib.sha256(self.input.read_bytes()).hexdigest()
        self.write_recipe(recipe)
        code, _, stderr = self.apply("--encoding", "gbk")
        self.assertEqual(code, 0, stderr)
        code, stdout, stderr = self.verify()
        self.assertEqual(code, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "verified")

    def test_verify_modified_file_returns_two(self):
        self.assertEqual(self.apply()[0], 0)
        output = self.target / "cleaned.csv"
        output.write_bytes(output.read_bytes().replace(b"0012345", b"9012345", 1))
        code, stdout, stderr = self.verify()
        self.assertEqual(code, 2, stderr)
        self.assertEqual(json.loads(stdout)["status"], "invalid")

    def test_verify_missing_file_returns_two(self):
        self.assertEqual(self.apply()[0], 0)
        (self.target / "flags.csv").unlink()
        code, stdout, stderr = self.verify()
        self.assertEqual(code, 2, stderr)
        self.assertEqual(json.loads(stdout)["status"], "invalid")

    def test_verify_changed_execution_returns_one(self):
        self.assertEqual(self.apply()[0], 0)
        execute = engine._execute

        def different_result(*args, **kwargs):
            outputs, summaries, warnings = execute(*args, **kwargs)
            outputs["cleaned.csv"] += b"\n"
            return outputs, summaries, warnings

        with patch("researchops_clean.engine._execute", side_effect=different_result):
            code, stdout, stderr = self.verify()
        self.assertEqual(code, 1, stderr)
        self.assertEqual(json.loads(stdout)["status"], "different")

    def test_missing_recipe_does_not_disclose_local_path(self):
        code, stdout, stderr = self.approve("--recipe", self.root / "private" / "missing.json")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertNotIn("private", stderr)
        self.assertTrue(json.loads(stderr)["error_code"])

    def test_invalid_arguments_do_not_echo_absolute_path(self):
        code, stdout, stderr = self.call("unknown-command", self.root / "private.csv")
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "cli_invalid_arguments")
        self.assertNotIn("private.csv", stderr)

    def test_help_has_all_four_commands(self):
        code, stdout, stderr = self.call("--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for command in ("ops", "approve", "apply", "verify"):
            self.assertIn(command, stdout)


if __name__ == "__main__":
    unittest.main()
