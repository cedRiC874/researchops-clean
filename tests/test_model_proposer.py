"""Offline workflow tests: one local request, private inputs, auditable output."""
from __future__ import annotations

import hashlib
import io
import json
import os
import socket
import sqlite3
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean import model_proposer
from researchops_clean.__main__ import main
from researchops_clean.issues import propose_from_profile
from researchops_clean.ledger import AuditLedger
from researchops_clean.model_proposer import ModelProposerError, run_model_proposal
from researchops_clean.ops import operation_catalog
from researchops_clean.profile import profile_csv
from researchops_clean.sensitive_patterns import find_sensitive_types

from model_fake_server import FakeModelServer, completion
from test_model_validation import mixed_proposals


FIXTURES = Path(__file__).parent / "fixtures"
SUCCESS_FILES = {
    "model_profile.json", "issues.json", "model_request.json", "model_response.json",
    "model_proposals.json", "draft_recipe.json", "run_record.json", "ledger.sqlite3",
}


class ModelProposalWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.source = self.root / "synthetic-private-source.csv"
        self.source.write_bytes((FIXTURES / "profile_workflow.csv").read_bytes())
        self.target = self.artifacts / "model"

    def run_model(self, server, *, target=None, **kwargs):
        return run_model_proposal(
            self.source, server.endpoint, "synthetic-request-model", target or self.target,
            project_root=self.root, **kwargs,
        )

    def read_json(self, name, target=None):
        return json.loads(((target or self.target) / name).read_text(encoding="utf-8"))

    def assert_valid_ledger(self, result, target=None):
        target = target or self.target
        self.assertFalse((target / "ledger.sqlite3-wal").exists())
        self.assertFalse((target / "ledger.sqlite3-shm").exists())
        ledger = AuditLedger(target / "ledger.sqlite3")
        self.assertTrue(ledger.verify_chain(result.record["run_id"]).valid)
        exported = ledger.export_run(result.record["run_id"])
        self.assertEqual(exported["run"]["mode"], "model_proposal")
        self.assertEqual(len(exported["model_calls"]), 1)
        self.assertEqual(exported["model_calls"][0]["provider"], "openai_compatible_local")
        self.assertTrue(result.record["chain_verification"]["valid"])
        text = json.dumps(exported, ensure_ascii=False)
        self.assertNotIn(str(self.root), text)
        self.assertNotIn(self.root.as_posix(), text)
        self.assertNotIn(self.source.name, text)
        return exported

    def test_success_one_request_mixed_results_hashes_and_exact_public_profiles(self):
        _, expected_model = profile_csv(self.source)
        expected_issues, rules = propose_from_profile(expected_model)
        response = completion(mixed_proposals(expected_model, rules))
        with FakeModelServer(response) as server:
            result = self.run_model(server)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(result.record["status"], "success")
        self.assertEqual(result.record["counts"], {"accepted": 3, "rejected": 7, "duplicates": 1})
        self.assertEqual({item.name for item in self.target.iterdir()}, SUCCESS_FILES)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(server.requests[0]["path"], "/v1/chat/completions")
        self.assertEqual((self.target / "model_request.json").read_bytes(), server.requests[0]["body"])
        self.assertEqual(self.read_json("model_profile.json"), expected_model)
        self.assertEqual(self.read_json("issues.json"), expected_issues)
        self.assertEqual(self.read_json("run_record.json"), result.record)
        self.assertEqual(self.read_json("model_proposals.json"), result.proposals)
        for kind in ("request", "response"):
            saved = (self.target / f"model_{kind}.json").read_bytes()
            self.assertEqual(result.record[f"{kind}_sha256"], hashlib.sha256(saved).hexdigest())
        self.assertEqual(result.record["requested_model"], "synthetic-request-model")
        self.assertEqual(result.record["response_model"], "synthetic-reply-model")
        self.assertEqual(result.record["host"], "127.0.0.1")
        self.assertIsInstance(result.record["port"], int)
        self.assertEqual(result.record["temperature"], 0)
        self.assertEqual(result.record["seed"], 0)
        self.assertEqual(result.record["max_tokens"], 4096)
        self.assertEqual(result.record["response_format"], "json_schema")
        self.assertEqual(result.record["finish_reason"], "stop")
        self.assertEqual(result.record["usage"], response["usage"])
        self.assertGreaterEqual(result.record["duration_ms"], 0)
        self.assertTrue(result.record["created_at_utc"])
        self.assertTrue(result.record["response_complete"])
        for path in self.target.glob("*.json"):
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", data)
            text = data.decode("utf-8")
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.root.as_posix(), text)
            self.assertNotIn(self.source.name, text)
        self.assert_valid_ledger(result)

    def test_original_schema_escaping_and_valid_evidence_pointer_false_positives_are_resolved(self):
        with FakeModelServer(completion()) as server:
            result = self.run_model(server)
        self.assertEqual(result.exit_code, 0)
        body = server.requests[0]["body"].decode("utf-8")
        # The historical raw-JSON scan flags escaped schema patterns/pointers.
        self.assertIn("file_path", find_sensitive_types(body))
        request = json.loads(body)
        user = json.loads(request["messages"][1]["content"])
        pointers = [
            item["pointer"] for issue in user["issues"]["issues"] for item in issue["evidence"]
        ]
        self.assertTrue(any("file_path" in find_sensitive_types(pointer) for pointer in pointers))
        self.assertEqual(user["operations"], [
            item for item in operation_catalog() if item["op"] != "range_check"
        ])
        self.assertNotIn("range_check", [item["op"] for item in user["operations"]])

    def test_request_exposes_only_public_projection_and_no_authorization(self):
        self.source.write_bytes((FIXTURES / "profile_privacy.csv").read_bytes())
        internal, public = profile_csv(self.source)
        internal_text = json.dumps(internal, ensure_ascii=False)
        self.assertIn("罕见紫鹭", internal_text)
        self.assertIn("110101199001011234", internal_text)
        with FakeModelServer(completion()) as server:
            result = self.run_model(server)
        self.assertEqual(result.exit_code, 0)
        captured = server.requests[0]
        self.assertNotIn("authorization", {key.lower() for key in captured["headers"]})
        text = captured["body"].decode("utf-8")
        for hidden in ("罕见紫鹭", "110101199001011234", "13800000000",
                       "synthetic@example.test", "raretoken", "sk-synthetic-only",
                       self.source.name, str(self.source), self.source.as_posix()):
            self.assertNotIn(hidden, text)
        user = json.loads(json.loads(text)["messages"][1]["content"])
        self.assertEqual(set(user), {"operations", "model_profile", "issues"})
        self.assertEqual(user["model_profile"], public)
        self.assertNotIn("internal_profile", text)

    def test_existing_disk_profiles_and_manual_recipe_are_never_read(self):
        poison = "private-manually-added-value"
        for directory in (self.root, self.artifacts):
            for filename in ("model_profile.json", "internal_profile.json",
                             "issues.json", "draft_recipe.json"):
                (directory / filename).write_text(poison, encoding="utf-8", newline="\n")
        with FakeModelServer(completion()) as server:
            result = self.run_model(server)
        self.assertEqual(result.exit_code, 0)
        self.assertNotIn(poison, server.requests[0]["body"].decode("utf-8"))
        self.assertNotIn(poison, (self.target / "draft_recipe.json").read_text(encoding="utf-8"))

    def test_system_proxy_and_environment_keys_are_not_used(self):
        synthetic_environment = {
            "HTTP_PROXY": "http://does-not-exist.invalid:1",
            "HTTPS_PROXY": "http://does-not-exist.invalid:1",
            "http_proxy": "http://does-not-exist.invalid:1",
            "https_proxy": "http://does-not-exist.invalid:1",
            "OPENAI_API_KEY": "synthetic-key-not-to-be-read",
        }
        with FakeModelServer(completion()) as server:
            with patch.object(os, "environ", synthetic_environment):
                result = self.run_model(server)
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(len(server.requests), 1)
        self.assertNotIn("authorization", {key.lower() for key in server.requests[0]["headers"]})
        self.assertNotIn("synthetic-key-not-to-be-read", server.requests[0]["body"].decode("utf-8"))

    def test_ledger_keeps_usage_but_not_prompt_or_answer_text(self):
        reason = "模型回复原文金丝雀合成内容"
        with FakeModelServer(completion([{
            "op": "normalize_text", "params": {"column": "性别"}, "reason": reason,
        }])) as server:
            result = self.run_model(server)
        exported = self.assert_valid_ledger(result)
        call = exported["model_calls"][0]
        self.assertEqual(call["input_tokens"], 12)
        self.assertEqual(call["output_tokens"], 7)
        text = json.dumps(exported, ensure_ascii=False)
        request = self.read_json("model_request.json")
        self.assertNotIn(request["messages"][0]["content"], text)
        self.assertNotIn(reason, text)
        self.assertNotIn('"messages"', text)
        self.assertNotIn('"proposals"', text)

    def test_response_format_none_and_repeated_response_have_deterministic_draft_and_report(self):
        proposal = {
            "op": "map_values", "params": {
                "column": "性别", "mapping": {"M": "男"}, "on_unmapped": "keep",
            }, "reason": "统一代码",
        }
        other = self.artifacts / "second"
        with FakeModelServer(completion([proposal])) as server:
            first = self.run_model(server, response_format="none")
            second = self.run_model(server, target=other, response_format="none")
        self.assertEqual((first.exit_code, second.exit_code), (0, 0))
        self.assertEqual(len(server.requests), 2)
        for captured in server.requests:
            self.assertNotIn("response_format", json.loads(captured["body"]))
        for filename in ("draft_recipe.json", "model_proposals.json"):
            self.assertEqual((self.target / filename).read_bytes(), (other / filename).read_bytes())

    def test_whole_output_failures_publish_audit_without_draft(self):
        cases = [
            (completion(content="不是 JSON"), "model_output_invalid"),
            (completion(content='先解释 {"proposals": []}'), "model_output_invalid"),
            (completion(content='{"proposals": []} 再解释'), "model_output_invalid"),
            (completion(content='{"proposals": [], "extra": true}'), "model_output_invalid"),
            (completion(content='{"proposals": []}', finish_reason="length"), "model_output_truncated"),
            (completion(content="[]"), "model_output_invalid"),
            (completion(content='{"proposals": []} {"proposals": []}'), "model_output_invalid"),
            ({"model": "synthetic", "choices": []}, "model_output_invalid"),
        ]
        for index, (body, error) in enumerate(cases):
            with self.subTest(index=index):
                target = self.artifacts / f"invalid-{index}"
                with FakeModelServer(body) as server:
                    result = self.run_model(server, target=target)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.record["status"], "failed")
                self.assertEqual(result.record["error_code"], error)
                self.assertFalse((target / "draft_recipe.json").exists())
                self.assertTrue((target / "model_request.json").is_file())
                self.assertTrue((target / "model_response.json").is_file())
                self.assert_valid_ledger(result, target)

    def test_non_json_http_response_is_preserved_as_text(self):
        body = b"synthetic service returned plain text"
        with FakeModelServer(body) as server:
            result = self.run_model(server)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.record["error_code"], "model_output_invalid")
        self.assertEqual((self.target / "model_response.txt").read_bytes(), body)
        self.assertFalse((self.target / "model_response.json").exists())
        self.assertFalse((self.target / "draft_recipe.json").exists())
        self.assertEqual(result.record["response_sha256"], hashlib.sha256(body).hexdigest())
        self.assert_valid_ledger(result)

    def test_nonfinite_and_unpaired_surrogate_content_leave_complete_failure_records(self):
        contents = (
            '{"proposals":[{"op":"normalize_text","params":'
            '{"column":"性别","unexpected":1e999},"reason":"合成建议"}]}',
            '{"proposals":[{"op":"normalize_text","params":'
            '{"column":"性别"},"reason":"\\ud800"}]}',
        )
        for index, content in enumerate(contents):
            with self.subTest(index=index):
                target = self.artifacts / f"invalid-unicode-number-{index}"
                with FakeModelServer(completion(content=content)) as server:
                    result = self.run_model(server, target=target)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.record["status"], "failed")
                self.assertEqual(result.record["error_code"], "model_output_invalid")
                self.assertEqual(self.read_json("run_record.json", target), result.record)
                self.assertTrue((target / "model_request.json").is_file())
                self.assertTrue((target / "model_response.json").is_file())
                self.assertFalse((target / "draft_recipe.json").exists())
                self.assert_valid_ledger(result, target)

    def test_http_500_redirect_timeout_and_oversize_publish_controlled_failures(self):
        cases = [
            ({"body": {"error": "synthetic"}, "status": 500}, {}, "model_http_error"),
            ({"body": b"synthetic redirect", "status": 302,
              "headers": {"Location": "http://example.invalid"}}, {}, "model_redirect_rejected"),
            ({"body": completion(), "delay": 0.15}, {"timeout": 0.02}, "model_timeout"),
            ({"body": b"x" * (1024 * 1024 + 1)}, {}, "model_response_too_large"),
        ]
        for index, (server_options, options, error) in enumerate(cases):
            with self.subTest(error=error):
                target = self.artifacts / f"transport-{index}"
                with FakeModelServer(**server_options) as server:
                    result = self.run_model(server, target=target, **options)
                self.assertEqual(len(server.requests), 1)
                self.assertEqual(result.exit_code, 3)
                self.assertEqual(result.record["status"], "failed")
                self.assertEqual(result.record["error_code"], error)
                self.assertFalse((target / "draft_recipe.json").exists())
                self.assertTrue((target / "model_request.json").exists())
                self.assert_valid_ledger(result, target)
                if error == "model_response_too_large":
                    self.assertFalse(result.record["response_complete"])
                    saved = next(target.glob("model_response.*"))
                    self.assertLessEqual(saved.stat().st_size, 1024 * 1024)

    def test_connection_failure_is_audited_with_no_response_or_draft(self):
        with socket.socket() as temporary:
            temporary.bind(("127.0.0.1", 0))
            port = temporary.getsockname()[1]
        result = run_model_proposal(
            self.source, f"http://127.0.0.1:{port}/v1", "synthetic-model", self.target,
            project_root=self.root, timeout=5,
        )
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(result.record["error_code"], "model_connection_error")
        self.assertFalse((self.target / "draft_recipe.json").exists())
        self.assertEqual(list(self.target.glob("model_response.*")), [])
        self.assertIsNone(result.record["response_sha256"])
        self.assert_valid_ledger(result)

    def test_disallowed_endpoints_are_rejected_before_any_request(self):
        with FakeModelServer(completion()) as server:
            endpoints = (
                "http://10.0.0.5:8000/v1", "https://api.deepseek.com/v1",
                server.endpoint.replace("127.0.0.1", "user:password@127.0.0.1"),
                server.endpoint + "?query=value", server.endpoint + "#fragment",
            )
            for endpoint in endpoints:
                with self.subTest(endpoint=endpoint):
                    with self.assertRaises(ModelProposerError) as caught:
                        run_model_proposal(
                            self.source, endpoint, "synthetic-model", self.target,
                            project_root=self.root,
                        )
                    self.assertEqual(caught.exception.exit_code, 2)
                    self.assertFalse(self.target.exists())
            self.assertEqual(server.requests, [])

    def test_invalid_input_output_and_oversize_request_are_rejected_before_send(self):
        with FakeModelServer(completion()) as server:
            for target in (self.root / "outside", self.artifacts):
                with self.subTest(target=target.name):
                    with self.assertRaises(ModelProposerError) as caught:
                        self.run_model(server, target=target)
                    self.assertEqual(caught.exception.exit_code, 2)
            self.target.mkdir()
            marker = self.target / "marker.txt"
            marker.write_bytes(b"synthetic-existing-output")
            with self.assertRaises(ModelProposerError):
                self.run_model(server)
            self.assertEqual(marker.read_bytes(), b"synthetic-existing-output")
            with self.assertRaises(ModelProposerError):
                run_model_proposal(
                    self.root / "missing.csv", server.endpoint, "synthetic-model",
                    self.artifacts / "missing-input", project_root=self.root,
                )
            with self.assertRaises(ModelProposerError) as caught:
                run_model_proposal(
                    self.source, server.endpoint, "x" * (256 * 1024),
                    self.artifacts / "oversize", project_root=self.root,
                )
            self.assertEqual(caught.exception.code, "model_request_too_large")
            self.assertEqual(server.requests, [])

    def test_sensitive_column_names_never_reach_the_server(self):
        with FakeModelServer(completion()) as server:
            for name in (r"C:\synthetic\private.csv", r"\\synthetic\share\private.csv",
                         "合成13800000000", "合成110101199001011234"):
                with self.subTest(name=name):
                    self.source.write_text(name + "\nA\n", encoding="utf-8", newline="\n")
                    with self.assertRaises(ModelProposerError) as caught:
                        self.run_model(server)
                    self.assertEqual(caught.exception.exit_code, 2)
            self.assertEqual(server.requests, [])
        self.assertFalse(self.target.exists())

    def test_tainted_profile_and_forged_evidence_are_blocked_before_send(self):
        original = model_proposer.build_request
        mutations = [
            ("profile-windows", lambda user: user["model_profile"].update(
                {"synthetic_extra": r"C:\synthetic\private.csv"})),
            ("profile-unc", lambda user: user["model_profile"].update(
                {"synthetic_extra": r"\\synthetic\share\private.csv"})),
            ("unresolved-pointer", lambda user: user["issues"]["issues"][0]["evidence"][0].update(
                {"pointer": "/columns/999999/kind"})),
            ("mismatched-pointer", lambda user: user["issues"]["issues"][0]["evidence"][0].update(
                {"pointer": "/columns/0/kind", "value": "fabricated"})),
            ("phone-string", lambda user: user["model_profile"].update(
                {"synthetic_extra": "13800000000"})),
            ("identity-key", lambda user: user["model_profile"].update(
                {"110101199001011234": True})),
        ]
        with FakeModelServer(completion()) as server:
            for name, mutate in mutations:
                def tainted(*args, **kwargs):
                    request = original(*args, **kwargs)
                    user = json.loads(request["messages"][1]["content"])
                    mutate(user)
                    request["messages"][1]["content"] = json.dumps(user, ensure_ascii=False)
                    return request
                with self.subTest(name=name):
                    with patch.object(model_proposer, "build_request", side_effect=tainted):
                        with self.assertRaises(ModelProposerError) as caught:
                            self.run_model(server)
                    self.assertEqual(caught.exception.exit_code, 2)
                    self.assertEqual(caught.exception.code, "model_request_sensitive")
            self.assertEqual(server.requests, [])
        self.assertFalse(self.target.exists())

    def test_gbk_input_keeps_original_byte_hash_in_public_projection(self):
        data = self.source.read_text(encoding="utf-8").encode("gbk")
        self.source.write_bytes(data)
        with FakeModelServer(completion()) as server:
            result = self.run_model(server, encoding="gbk")
        self.assertEqual(result.exit_code, 0)
        self.assertEqual(self.read_json("model_profile.json")["input"]["sha256"],
                         hashlib.sha256(data).hexdigest())

    def test_sqlite_failures_use_dispatch_aware_exit_codes_and_clean_staging(self):
        cases = (("AuditLedger", 2, 0), ("_finish_ledger", 3, 1))
        for patched_name, exit_code, request_count in cases:
            with self.subTest(patched_name=patched_name):
                stderr = io.StringIO()
                with FakeModelServer(completion()) as server:
                    with patch.object(
                        model_proposer, patched_name,
                        side_effect=sqlite3.OperationalError("synthetic database failure"),
                    ):
                        with redirect_stderr(stderr):
                            with self.assertRaises(ModelProposerError) as caught:
                                self.run_model(server)
                self.assertEqual(caught.exception.exit_code, exit_code)
                self.assertEqual(caught.exception.code, "model_record_write_failed")
                self.assertEqual(len(server.requests), request_count)
                self.assertEqual(stderr.getvalue(), "")
                self.assertTrue(caught.exception.__suppress_context__)
                self.assertNotIn("synthetic database failure", str(caught.exception))
                self.assertFalse(self.target.exists())
                self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_cleanup_failure_after_dispatch_is_a_controlled_exit_three(self):
        with FakeModelServer(completion()) as server:
            with patch.object(
                model_proposer, "_rename_without_replacement",
                side_effect=OSError("synthetic rename failure"),
            ), patch.object(
                model_proposer.shutil, "rmtree",
                side_effect=OSError("synthetic cleanup failure"),
            ):
                with self.assertRaises(ModelProposerError) as caught:
                    self.run_model(server)
        self.assertEqual(len(server.requests), 1)
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(caught.exception.code, "output_cleanup_failed")
        self.assertTrue(caught.exception.__suppress_context__)
        self.assertNotIn("synthetic cleanup failure", str(caught.exception))
        self.assertFalse(self.target.exists())
        # The temporary-directory cleanup runs after the injected failure ends.
        remaining = list(self.artifacts.iterdir())
        self.assertEqual(len(remaining), 1)
        self.assertTrue(remaining[0].is_dir())


class ModelProposalEndToEndTests(unittest.TestCase):
    def test_model_propose_human_fill_approve_apply_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            source = (FIXTURES / "profile_workflow.csv").resolve()
            proposed = root / "artifacts" / "model"
            cleaned = root / "artifacts" / "cleaned"
            filled = root / "filled.json"
            approved = root / "approved.json"

            def cli(*args):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("researchops_clean.__main__.Path.cwd", return_value=root):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main([str(arg) for arg in args])
                return code, stdout.getvalue(), stderr.getvalue()

            response = completion([{
                "op": "normalize_text", "params": {"column": "性别"},
                "reason": "保留待人工审批的模型建议",
            }])
            with FakeModelServer(response) as server:
                code, _, error = cli(
                    "model-propose", "--input", source, "--endpoint", server.endpoint,
                    "--model", "synthetic-model", "--output-dir", proposed,
                )
            self.assertEqual(code, 0, error)
            self.assertEqual(len(server.requests), 1)
            draft = json.loads((proposed / "draft_recipe.json").read_text(encoding="utf-8"))
            self.assertTrue(any(step["step_id"] == "M1" for step in draft["steps"]))
            self.assertTrue(all(step["approval"]["status"] == "pending" for step in draft["steps"]))
            code, _, error = cli(
                "apply", "--input", source, "--recipe", proposed / "draft_recipe.json",
                "--output-dir", cleaned,
            )
            self.assertEqual(code, 2)
            self.assertIn("recipe_step_not_approved", error)
            self.assertFalse(cleaned.exists())
            human_mapping = {"M": "男", "男": "男", "F": "女", "女": "女", "A": "A", " Ａ ": "A"}
            for step in draft["steps"]:
                if step["op"] == "map_values":
                    for value in step["params"]["mapping"]:
                        step["params"]["mapping"][value] = human_mapping[value]
                if step["op"] == "sentinel_to_missing" and "values" not in step["params"]:
                    step["params"]["values"] = ["999"]
            filled.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8", newline="\n")
            code, _, error = cli(
                "approve", "--recipe", filled,
                "--steps", ",".join(step["step_id"] for step in draft["steps"]),
                "--reviewer", "synthetic-human-reviewer", "--output", approved,
            )
            self.assertEqual(code, 0, error)
            code, _, error = cli(
                "apply", "--input", source, "--recipe", approved, "--output-dir", cleaned,
            )
            self.assertEqual(code, 0, error)
            code, stdout, error = cli(
                "verify", "--input", source, "--recipe", approved, "--run-dir", cleaned,
            )
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(stdout)["status"], "verified")


if __name__ == "__main__":
    unittest.main()
