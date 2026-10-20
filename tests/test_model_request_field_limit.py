"""Decoded-field length limits without changing the sensitive-data detector."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean import model_client, model_proposer
from researchops_clean.__main__ import main
from researchops_clean.sensitive_patterns import find_sensitive_types

from model_fake_server import FakeModelServer, completion
from test_model_client import change_user, safe_request


class ModelRequestFieldLimitTests(unittest.TestCase):
    def test_2001_character_keys_and_values_are_refused_before_their_regex_scan(self):
        oversized = "文" * 2001
        placements = (
            lambda request: request.update({oversized: "safe"}),
            lambda request: request.update(model=oversized),
            lambda request: request["messages"][0].update(content=oversized),
            lambda request: change_user(
                request, lambda user: user["model_profile"].update({oversized: 1}),
            ),
            lambda request: change_user(
                request, lambda user: user["model_profile"].update(note=oversized),
            ),
            lambda request: change_user(
                request, lambda user: user["model_profile"].update(notes=[oversized]),
            ),
        )
        for placement in placements:
            with self.subTest(placement=placements.index(placement)):
                request = safe_request()
                placement(request)
                with patch("researchops_clean.model_client.find_sensitive_types",
                           wraps=find_sensitive_types) as detector:
                    with self.assertRaises(model_client.ModelClientError) as caught:
                        model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
                self.assertEqual(caught.exception.code, "request_field_too_long")
                self.assertNotIn(oversized, [call.args[0] for call in detector.call_args_list])

    def test_2000_character_keys_and_values_receive_complete_regular_detection(self):
        boundary = "文" * 2000
        request = safe_request()
        request[boundary] = boundary
        change_user(request, lambda user: user["model_profile"].update({boundary: boundary}))
        with patch("researchops_clean.model_client.find_sensitive_types",
                   wraps=find_sensitive_types) as detector:
            checked = model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
        self.assertEqual(json.loads(checked), request)
        self.assertEqual(sum(call.args == (boundary,) for call in detector.call_args_list), 4)

    def test_sensitive_text_at_2000_characters_is_still_rejected(self):
        sensitive = "文" * (2000 - 11) + "13800000000"
        self.assertEqual(len(sensitive), 2000)
        request = change_user(
            safe_request(), lambda user: user["model_profile"].update(note=sensitive),
        )
        with patch("researchops_clean.model_client.find_sensitive_types",
                   wraps=find_sensitive_types) as detector:
            with self.assertRaises(model_client.ModelClientError) as caught:
                model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
        self.assertEqual(caught.exception.code, "model_request_sensitive")
        self.assertIn(sensitive, [call.args[0] for call in detector.call_args_list])

    def test_length_is_checked_after_json_decoding_without_limiting_the_container(self):
        boundary = "文" * 2000
        request = change_user(safe_request(), lambda user: user["model_profile"].update(note=boundary))
        user = json.loads(request["messages"][1]["content"])
        request["messages"][1]["content"] = json.dumps(user, ensure_ascii=True)
        self.assertGreater(len(request["messages"][1]["content"]), 2000)
        model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
        user["model_profile"]["note"] += "文"
        request["messages"][1]["content"] = json.dumps(user, ensure_ascii=True)
        with self.assertRaises(model_client.ModelClientError) as caught:
            model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
        self.assertEqual(caught.exception.code, "request_field_too_long")

    def test_verified_pointer_cannot_bypass_the_field_length_limit(self):
        long_key = "文" * 1990
        pointer = "/columns/0/" + long_key
        self.assertEqual(len(pointer), 2001)
        request = change_user(safe_request(), lambda user: (
            user["model_profile"]["columns"][0].update({long_key: "safe"}),
            user["issues"]["issues"][0]["evidence"][0].update(pointer=pointer, value="safe"),
        ))
        with self.assertRaises(model_client.ModelClientError) as caught:
            model_client.encode_and_validate_request(request, input_filename="synthetic.csv")
        self.assertEqual(caught.exception.code, "request_field_too_long")

    def test_cli_refuses_2001_characters_with_exit_two_and_no_fake_request(self):
        build = model_proposer.build_request
        for as_key in (False, True):
            with self.subTest(as_key=as_key), tempfile.TemporaryDirectory() as temporary:
                root = Path(temporary)
                (root / "artifacts").mkdir()
                target = root / "artifacts" / "model"
                source = (Path(__file__).parent / "fixtures" / "profile_workflow.csv").resolve()

                def oversized_request(*args, **kwargs):
                    request = build(*args, **kwargs)
                    oversized = "文" * 2001
                    return change_user(request, lambda user: user["model_profile"].update(
                        {oversized: "safe"} if as_key else {"note": oversized}
                    ))

                with FakeModelServer(completion()) as server:
                    stdout, stderr = io.StringIO(), io.StringIO()
                    with patch("researchops_clean.model_proposer.build_request", side_effect=oversized_request):
                        with patch("researchops_clean.__main__.Path.cwd", return_value=root):
                            with redirect_stdout(stdout), redirect_stderr(stderr):
                                code = main([
                                    "model-propose", "--input", str(source),
                                    "--endpoint", server.endpoint, "--model", "synthetic-model",
                                    "--output-dir", str(target),
                                ])
                    self.assertEqual(code, 2)
                    self.assertEqual(json.loads(stderr.getvalue())["error_code"], "request_field_too_long")
                    self.assertEqual(stdout.getvalue(), "")
                    self.assertEqual(server.requests, [])
                    self.assertFalse(target.exists())
