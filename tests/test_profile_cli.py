"""New offline commands preserve the structured, path-free CLI contract."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean.profile_workflow import ProfileWorkflowError


FIXTURES = Path(__file__).parent / "fixtures"


class ProfileCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()
        self.input = self.root / "data.csv"
        self.input.write_bytes((FIXTURES / "lab_example.csv").read_bytes())
        self.output = self.root / "artifacts" / "profile-01"

    def call(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([str(argument) for argument in arguments])
        self.assertNotIn(str(self.root), stdout.getvalue() + stderr.getvalue())
        return result, stdout.getvalue(), stderr.getvalue()

    def run_command(self, command, *extra):
        return self.call(command, "--input", self.input, "--output-dir", self.output, *extra)

    def test_help_lists_both_new_commands(self):
        code, stdout, stderr = self.call("--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for command in ("profile", "propose"):
            self.assertIn(command, stdout)

    def test_profile_publishes_two_files_and_prints_counts(self):
        code, stdout, stderr = self.run_command("profile")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        summary = json.loads(stdout)
        model = json.loads((self.output / "model_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, {
            "status": "success",
            "row_count": model["input"]["row_count"],
            "column_count": len(model["input"]["columns"]),
            "issue_count": 0,
            "category_counts": {"符合性": 0, "完整性": 0, "合理性": 0},
        })
        self.assertEqual(
            {path.name for path in self.output.iterdir()},
            {"internal_profile.json", "model_profile.json"},
        )

    def test_propose_prints_counts_matching_published_cards(self):
        code, stdout, stderr = self.run_command("propose")
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        summary = json.loads(stdout)
        cards = json.loads((self.output / "issues.json").read_text(encoding="utf-8"))["issues"]
        self.assertEqual(summary["status"], "success")
        self.assertGreater(summary["issue_count"], 0)
        self.assertEqual(summary["issue_count"], len(cards))
        self.assertEqual(summary["category_counts"], {
            category: sum(card["category"] == category for card in cards)
            for category in ("符合性", "完整性", "合理性")
        })
        self.assertEqual(
            {path.name for path in self.output.iterdir()},
            {"internal_profile.json", "model_profile.json", "issues.json", "draft_recipe.json"},
        )

    def test_parameters_reach_workflow_without_affecting_old_commands(self):
        summary = {
            "row_count": 1, "column_count": 1, "issue_count": 0,
            "category_counts": {"符合性": 0, "完整性": 0, "合理性": 0},
        }
        for command, extra, k, encoding in (
            ("profile", (), 5, "utf-8-sig"),
            ("propose", ("--k", "9", "--encoding", "gb18030"), 9, "gb18030"),
        ):
            with self.subTest(command=command):
                with patch("researchops_clean.profile_workflow.run_profile",
                           return_value=summary) as workflow:
                    code, stdout, stderr = self.run_command(command, *extra)
                self.assertEqual(code, 0, stderr)
                self.assertEqual(json.loads(stdout), {"status": "success", **summary})
                workflow.assert_called_once_with(
                    self.input, self.output, project_root=self.root,
                    k=k, encoding=encoding, propose=command == "propose",
                )

    def test_invalid_k_is_rejected_before_workflow(self):
        for command in ("profile", "propose"):
            for value in ("4", "0", "-1", "5.0", "five"):
                with self.subTest(command=command, k=value):
                    with patch("researchops_clean.profile_workflow.run_profile") as workflow:
                        code, stdout, stderr = self.run_command(command, "--k", value)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(json.loads(stderr)["status"], "error")
                    workflow.assert_not_called()
        self.assertFalse(self.output.exists())

    def test_unsupported_encoding_is_a_structured_argument_error(self):
        for command in ("profile", "propose"):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_command(command, "--encoding", "latin-1")
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["error_code"], "cli_invalid_arguments")
        self.assertFalse(self.output.exists())

    def test_workflow_errors_keep_structured_chinese_messages(self):
        for command in ("profile", "propose"):
            with self.subTest(command=command):
                with patch(
                    "researchops_clean.profile_workflow.run_profile",
                    side_effect=ProfileWorkflowError("profile_test_error", "合成测试拒绝。"),
                ):
                    code, stdout, stderr = self.run_command(command)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr), {
                    "status": "error", "error_code": "profile_test_error",
                    "message": "合成测试拒绝。",
                })

    def test_missing_required_arguments_are_structured_errors(self):
        for command in ("profile", "propose"):
            with self.subTest(command=command):
                code, stdout, stderr = self.call(command, "--input", self.input)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["error_code"], "cli_invalid_arguments")

    def test_missing_input_does_not_echo_filename_or_path(self):
        for command in ("profile", "propose"):
            with self.subTest(command=command):
                code, stdout, stderr = self.run_command(
                    command, "--input", self.root / "private-name.csv"
                )
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "error")
                self.assertNotIn("private-name.csv", stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
