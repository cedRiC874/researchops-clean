"""CLI contracts use only a test-owned loopback fake model service."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from model_fake_server import FakeModelServer, completion
from researchops_clean.__main__ import main


class ModelCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        (self.root / "artifacts").mkdir()
        self.input = self.root / "synthetic_cli_input.csv"
        self.input.write_text("分组\n甲\n甲\n甲\n甲\n甲\n", encoding="utf-8", newline="\n")
        self.output = self.root / "artifacts" / "model-01"

    def call(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                result = main([str(argument) for argument in arguments])
        rendered = stdout.getvalue() + stderr.getvalue()
        self.assertNotIn(str(self.root), rendered)
        self.assertNotIn(self.root.as_posix(), rendered)
        self.assertNotIn(self.input.name, rendered)
        return result, stdout.getvalue(), stderr.getvalue()

    def propose(self, server, *extra):
        return self.call(
            "model-propose", "--input", self.input, "--endpoint", server.endpoint,
            "--model", "fake-model", "--output-dir", self.output, *extra,
        )

    def test_help_lists_model_command_and_its_options(self):
        code, stdout, stderr = self.call("--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("model-propose", stdout)
        code, stdout, stderr = self.call("model-propose", "--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        for option in (
            "--input", "--endpoint", "--model", "--output-dir",
            "--encoding", "--timeout", "--max-tokens", "--response-format",
        ):
            self.assertIn(option, stdout)

    def test_success_uses_defaults_and_prints_only_public_summary(self):
        with FakeModelServer(completion()) as server:
            code, stdout, stderr = self.propose(server)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(len(server.requests), 1)
        request = json.loads(server.requests[0]["body"])
        self.assertEqual(request["model"], "fake-model")
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["seed"], 0)
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(request["response_format"]["type"], "json_schema")
        self.assertTrue(request["response_format"]["json_schema"]["strict"])
        summary = json.loads(stdout)
        record = json.loads((self.output / "run_record.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, {
            "status": "success", "run_id": record["run_id"],
            "counts": {"accepted": 0, "rejected": 0, "duplicates": 0},
        })
        self.assertTrue((self.output / "draft_recipe.json").is_file())

    def test_optional_encoding_tokens_timeout_and_response_format(self):
        self.input.write_bytes("分组\n甲\n甲\n甲\n甲\n甲\n".encode("gbk"))
        with FakeModelServer(completion()) as server:
            code, _, stderr = self.propose(
                server, "--encoding", "gbk", "--max-tokens", "512",
                "--timeout", "2.5", "--response-format", "none",
            )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(len(server.requests), 1)
        request = json.loads(server.requests[0]["body"])
        self.assertEqual(request["max_tokens"], 512)
        self.assertNotIn("response_format", request)

    def test_http_failure_returns_three_and_preserves_failure_record(self):
        with FakeModelServer({"error": "synthetic failure"}, status=500) as server:
            code, stdout, stderr = self.propose(server)
        self.assertEqual(code, 3, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(len(server.requests), 1)
        summary = json.loads(stdout)
        record = json.loads((self.output / "run_record.json").read_text(encoding="utf-8"))
        self.assertEqual(summary, {
            "status": "failed", "run_id": record["run_id"], "counts": record["counts"],
            "error_code": record["error_code"],
        })
        self.assertFalse((self.output / "draft_recipe.json").exists())
        self.assertTrue((self.output / "model_request.json").is_file())
        self.assertTrue((self.output / "ledger.sqlite3").is_file())

    def test_invalid_model_content_returns_three_and_no_draft(self):
        with FakeModelServer(completion(content="这是非 JSON 的合成回答")) as server:
            code, stdout, stderr = self.propose(server)
        self.assertEqual(code, 3, stderr)
        self.assertEqual(stderr, "")
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(json.loads(stdout)["error_code"], "model_output_invalid")
        self.assertEqual(json.loads(stdout)["status"], "failed")
        self.assertFalse((self.output / "draft_recipe.json").exists())
        self.assertTrue((self.output / "run_record.json").is_file())

    def test_publication_failure_after_request_returns_three(self):
        with FakeModelServer(completion()) as server:
            with patch(
                "researchops_clean.model_proposer._rename_without_replacement",
                side_effect=OSError("synthetic publication failure"),
            ):
                code, stdout, stderr = self.propose(server)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(code, 3)
        self.assertEqual(stdout, "")
        error = json.loads(stderr)
        self.assertEqual(error["status"], "error")
        self.assertTrue(error["error_code"])
        self.assertFalse(self.output.exists())

    def test_invalid_options_return_two_without_sending(self):
        cases = (
            ("--encoding", "latin-1"),
            ("--response-format", "invalid"),
            ("--timeout", "not-a-number"),
            ("--timeout", "601"),
            ("--max-tokens", "not-an-integer"),
        )
        with FakeModelServer(completion()) as server:
            for option, value in cases:
                with self.subTest(option=option, value=value):
                    code, stdout, stderr = self.propose(server, option, value)
                    self.assertEqual(code, 2)
                    self.assertEqual(stdout, "")
                    self.assertEqual(json.loads(stderr)["status"], "error")
            self.assertEqual(server.requests, [])
        self.assertFalse(self.output.exists())

    def test_missing_required_option_returns_two_without_sending(self):
        with FakeModelServer(completion()) as server:
            code, stdout, stderr = self.call(
                "model-propose", "--input", self.input, "--endpoint", server.endpoint,
                "--output-dir", self.output,
            )
            self.assertEqual(server.requests, [])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "cli_invalid_arguments")
        self.assertFalse(self.output.exists())

    def test_endpoint_query_is_rejected_before_sending(self):
        with FakeModelServer(completion()) as server:
            code, stdout, stderr = self.propose(
                server, "--endpoint", server.endpoint + "?synthetic=1"
            )
            self.assertEqual(server.requests, [])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertFalse(self.output.exists())

    def test_existing_output_is_preserved_without_sending(self):
        self.output.mkdir()
        marker = self.output / "preserve.txt"
        marker.write_bytes(b"synthetic marker\n")
        with FakeModelServer(completion()) as server:
            code, stdout, stderr = self.propose(server)
            self.assertEqual(server.requests, [])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["error_code"], "output_exists")
        self.assertEqual(marker.read_bytes(), b"synthetic marker\n")
        self.assertEqual(list(self.output.iterdir()), [marker])

    def test_missing_input_is_path_free_and_sends_nothing(self):
        with FakeModelServer(completion()) as server:
            code, stdout, stderr = self.propose(
                server, "--input", self.root / "private-missing-input.csv"
            )
            self.assertEqual(server.requests, [])
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertNotIn("private-missing-input.csv", stderr)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
