from __future__ import annotations

import json
import os
import socket
import unittest
from unittest.mock import patch

from jsonschema import Draft202012Validator

from researchops_clean.model_client import (
    MAX_REQUEST_BYTES,
    MAX_RESPONSE_BYTES,
    PROPOSALS_SCHEMA,
    SYSTEM_PROMPT,
    ModelClientError,
    build_request,
    encode_and_validate_request,
    post_request,
    validate_endpoint,
    validate_options,
)
from researchops_clean.ops import operation_catalog
from researchops_clean.sensitive_patterns import find_sensitive_types

from model_fake_server import FakeModelServer


def safe_request():
    profile = {"columns": [{"name": "性别", "kind": "code_like", "values": [{"value": "男", "count": 5}]}]}
    issues = {
        "schema_version": "cleaning-issues/1",
        "issues": [{
            "issue_id": "I1",
            "evidence": [{"pointer": "/columns/0/kind", "value": "code_like"}],
        }],
    }
    return build_request("synthetic-model", profile, issues)


def change_user(request, change):
    user = json.loads(request["messages"][1]["content"])
    change(user)
    request["messages"][1]["content"] = json.dumps(user, ensure_ascii=False)
    return request


class ModelClientRequestTests(unittest.TestCase):
    def assert_rejected(self, request, code="model_request_sensitive", **kwargs):
        with self.assertRaises(ModelClientError) as error:
            encode_and_validate_request(request, input_filename="synthetic.csv", **kwargs)
        self.assertEqual(error.exception.code, code)
        self.assertNotIn("C:\\", error.exception.message)

    def test_build_request_exact_payload_operations_and_deterministic_settings(self):
        request = safe_request()
        self.assertEqual(request["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        user = json.loads(request["messages"][1]["content"])
        self.assertEqual(set(user), {"operations", "model_profile", "issues"})
        self.assertEqual(user["operations"], [item for item in operation_catalog() if item["op"] != "range_check"])
        self.assertEqual(request["temperature"], 0)
        self.assertEqual(request["seed"], 0)
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(request["response_format"], {
            "type": "json_schema",
            "json_schema": {"name": "cleaning_proposals", "schema": PROPOSALS_SCHEMA, "strict": True},
        })
        self.assertEqual(encode_and_validate_request(request, input_filename="synthetic.csv"),
                         encode_and_validate_request(safe_request(), input_filename="synthetic.csv"))

    def test_none_response_format_and_independent_schema(self):
        self.assertNotIn("response_format", build_request("test", {}, {}, response_format="none"))
        first = safe_request()
        first["response_format"]["json_schema"]["schema"]["properties"].clear()
        self.assertIn("proposals", safe_request()["response_format"]["json_schema"]["schema"]["properties"])

    def test_output_schema_keeps_reason_validation_per_proposal(self):
        validator = Draft202012Validator(PROPOSALS_SCHEMA)
        for reason in ("", "文" * 201):
            self.assertTrue(validator.is_valid({"proposals": [{"op": "x", "params": {}, "reason": reason}]}))
        for value in (
            {"proposals": [{}]},
            {"proposals": [{"op": "x", "params": {}, "reason": "理由", "extra": 1}]},
            {"proposals": [], "extra": 1},
            {"proposals": [{"op": "x", "params": {}, "reason": "理由"}] * 31},
        ):
            self.assertFalse(validator.is_valid(value))

    def test_both_known_false_positives_pass_without_changing_patterns(self):
        request = safe_request()
        encoded = json.dumps(request, ensure_ascii=False)
        self.assertIn("file_path", find_sensitive_types(encoded))
        self.assertIn("file_path", find_sensitive_types("/columns/0/kind"))
        checked = encode_and_validate_request(request, input_filename="synthetic.csv")
        self.assertEqual(json.loads(checked), request)

    def test_all_other_sensitive_types_in_any_decoded_key_or_string_are_rejected(self):
        sensitive = ("110101199001011234", "13800138000", "fake@example.invalid", "sk-synthetic-test")
        placements = (
            lambda req, value: req.update({value: "safe"}),
            lambda req, value: req.update({"model": value}),
            lambda req, value: req["messages"][0].update({"content": value}),
            lambda req, value: req["response_format"].update({value: "safe"}),
            lambda req, value: req["response_format"]["json_schema"].update({"name": value}),
            lambda req, value: change_user(req, lambda user: user["model_profile"].update({value: "safe"})),
            lambda req, value: change_user(req, lambda user: user["model_profile"].update({"text": value})),
            lambda req, value: change_user(req, lambda user: user["issues"]["issues"][0].update({"reason": value})),
            lambda req, value: change_user(req, lambda user: user["operations"][0].update({"description": value})),
            lambda req, value: change_user(req, lambda user: user["operations"][0]["params_schema"].update({value: "safe"})),
            lambda req, value: change_user(req, lambda user: user["issues"]["issues"][0]["evidence"][0].update({"value": value})),
        )
        for value in sensitive:
            for index, place in enumerate(placements):
                with self.subTest(value=value, placement=index):
                    request = safe_request()
                    place(request, value)
                    self.assert_rejected(request)

    def test_windows_unc_and_posix_paths_are_rejected_in_column_names_and_profile(self):
        paths = (r"C:\private\synthetic.csv", r"\\synthetic-host\private\table.csv", "/private/data")
        for value in paths:
            for field in ("name", "comment"):
                with self.subTest(value=value, field=field):
                    request = change_user(safe_request(), lambda user: user["model_profile"]["columns"][0].update({field: value}))
                    self.assert_rejected(request)

    def test_unresolvable_mismatched_and_noncanonical_pointer_are_not_exempt(self):
        cases = (
            ("/columns/0/absent", "code_like"),
            ("/columns/0/kind", "numeric"),
            ("/columns/00/kind", "code_like"),
            ("/columns/-1/kind", "code_like"),
            ("/columns/0/kind~2", "code_like"),
            ("/columns/0/kind", True),
        )
        for pointer, value in cases:
            with self.subTest(pointer=pointer, value=value):
                request = change_user(safe_request(), lambda user: user["issues"]["issues"][0]["evidence"][0].update(
                    {"pointer": pointer, "value": value}))
                self.assert_rejected(request)

    def test_valid_pointer_outside_exact_evidence_field_is_not_exempt(self):
        placements = (
            lambda user: user["model_profile"].update({"pointer": "/columns/0/kind", "value": "code_like"}),
            lambda user: user["issues"]["issues"][0].update({"pointer": "/columns/0/kind", "value": "code_like"}),
            lambda user: user["issues"]["issues"][0]["evidence"][0].update({"other": "/columns/0/kind"}),
            lambda user: user["issues"].update({"evidence": [{"pointer": "/columns/0/kind", "value": "code_like"}]}),
            lambda user: user.update({"evidence": [{"pointer": "/columns/0/kind", "value": "code_like"}]}),
        )
        for index, place in enumerate(placements):
            with self.subTest(placement=index):
                self.assert_rejected(change_user(safe_request(), place))

    def test_unicode_normalization_equivalence_does_not_match_evidence_value(self):
        def mutate(user):
            user["model_profile"]["columns"][0]["name"] = "\u00e9"
            user["issues"]["issues"][0]["evidence"][0] = {
                "pointer": "/columns/0/name", "value": "e\u0301",
            }
        self.assert_rejected(change_user(safe_request(), mutate))

    def test_pointer_matching_value_does_not_exempt_sensitive_keys_or_value(self):
        for sensitive in ("110101199001011234", "13800138000", "fake@example.invalid", "sk-synthetic-test"):
            for mode in ("key", "value"):
                with self.subTest(sensitive=sensitive, mode=mode):
                    def mutate(user):
                        if mode == "key":
                            user["model_profile"]["columns"][0][sensitive] = "safe"
                            proof = {"pointer": f"/columns/0/{sensitive}", "value": "safe"}
                        else:
                            user["model_profile"]["columns"][0]["extra"] = sensitive
                            proof = {"pointer": "/columns/0/extra", "value": sensitive}
                        user["issues"]["issues"][0]["evidence"][0] = proof
                    self.assert_rejected(change_user(safe_request(), mutate))

    def test_valid_evidence_pointer_still_runs_non_path_sensitive_checks(self):
        # The mock only suppresses profile keys' independent match; the real
        # detector must still reject the same sensitive text in the pointer.
        for sensitive in ("110101199001011234", "13800138000", "fake@example.invalid", "sk-synthetic-test"):
            def mutate(user):
                user["model_profile"]["columns"][0][sensitive] = "safe"
                user["issues"]["issues"][0]["evidence"][0] = {
                    "pointer": f"/columns/0/{sensitive}", "value": "safe",
                }
            request = change_user(safe_request(), mutate)
            with patch("researchops_clean.model_client.find_sensitive_types",
                       side_effect=lambda text: [] if text == sensitive else find_sensitive_types(text)):
                self.assert_rejected(request)

    def test_evidence_pointer_keys_and_values_are_both_scanned(self):
        for value in (r"C:\private\data", r"\\synthetic-host\share"):
            def mutate(user):
                user["model_profile"]["columns"][0]["extra"] = value
                user["issues"]["issues"][0]["evidence"][0] = {"pointer": "/columns/0/extra", "value": value}
            self.assert_rejected(change_user(safe_request(), mutate))
        request = change_user(safe_request(), lambda user: user["issues"]["issues"][0]["evidence"][0].update(
            {"/columns/0/kind": "code_like"}))
        self.assert_rejected(request)

    def test_json_pointer_escape_rules_and_numeric_types(self):
        def mutate(user):
            user["model_profile"]["a/b"] = {"~": [5]}
            user["issues"]["issues"][0]["evidence"][0] = {"pointer": "/a~1b/~0/0", "value": 5}
        encode_and_validate_request(change_user(safe_request(), mutate), input_filename="synthetic.csv")
        for proof_value in (True, "5", 5.0):
            request = change_user(safe_request(), lambda user: (
                user["model_profile"].update({"counts": {"n": 5}}),
                user["issues"]["issues"][0]["evidence"][0].update({"pointer": "/counts/n", "value": proof_value}),
            ))
            self.assert_rejected(request)

    def test_input_filename_and_forbidden_paths_are_checked_after_decoding(self):
        for text in ("synthetic.csv", "/private", "folder\\synthetic.csv"):
            request = change_user(safe_request(), lambda user: user["model_profile"].update({"note": text}))
            self.assert_rejected(request, forbidden_paths=("/private",))
        request = change_user(safe_request(), lambda user: user["model_profile"].update({"synthetic.csv": 1}))
        self.assert_rejected(request)
        request = safe_request()
        request["messages"][1]["content"] = request["messages"][1]["content"].replace(
            '"code_like"', '"\\u0073ynthetic.csv"', 1)
        self.assert_rejected(request)

    def test_numbers_booleans_and_null_are_not_sensitive_text(self):
        request = change_user(safe_request(), lambda user: user["model_profile"].update(
            {"number": 13800138000, "count": 110101199001011234, "enabled": True, "missing": None}))
        encode_and_validate_request(request, input_filename="synthetic.csv")

    def test_request_limit_measures_actual_utf8_bytes(self):
        request = safe_request()
        request["model"] = "文" * (MAX_REQUEST_BYTES // 3)
        self.assert_rejected(request, "model_request_too_large")
        request = safe_request()
        request["padding"] = []
        base_size = len(encode_and_validate_request(request, input_filename="synthetic.csv"))
        request["padding"] = ["a" * 20] * ((MAX_REQUEST_BYTES - base_size) // 23 - 1)
        remaining = MAX_REQUEST_BYTES - len(json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        ).encode("utf-8"))
        request["padding"].append("a" * (remaining - 3))
        self.assertEqual(len(encode_and_validate_request(request, input_filename="synthetic.csv")), MAX_REQUEST_BYTES)
        request["padding"][-1] += "a"
        self.assert_rejected(request, "model_request_too_large")

    def test_invalid_nested_json_is_rejected_and_duplicate_keys_cannot_hide_values(self):
        for content in (
            "not json", "[]", '{"bad": "13800138000", "bad": "safe"}',
            '{"value": NaN}', '{"value": Infinity}',
        ):
            request = safe_request()
            request["messages"][1]["content"] = content
            self.assert_rejected(request, "model_request_invalid")

    def test_endpoint_validation_normalizes_only_allowed_loopback_endpoints(self):
        cases = (
            ("http://127.0.0.1:8123/v1", "127.0.0.1", 8123, "http://127.0.0.1:8123/v1/chat/completions"),
            ("https://localhost/v1/", "localhost", 443, "https://localhost/v1/chat/completions"),
            ("http://[::1]:8123", "::1", 8123, "http://[::1]:8123/chat/completions"),
            ("http://localhost", "localhost", 80, "http://localhost/chat/completions"),
        )
        for raw, host, port, url in cases:
            with self.subTest(raw=raw):
                endpoint = validate_endpoint(raw)
                self.assertEqual((endpoint.host, endpoint.port, endpoint.url), (host, port, url))

    def test_endpoint_rejects_external_addresses_credentials_and_url_ambiguity(self):
        for endpoint in (
            "http://10.0.0.5:8000/v1", "https://api.deepseek.com/v1",
            "http://user:secret@127.0.0.1/v1", "http://@localhost/v1",
            "http://localhost/v1?x=1", "http://localhost/v1?", "http://localhost/v1#",
            "http://localhost/v1#fragment", "ftp://localhost/v1", "file:///local",
            "http://127.0.0.2/v1", "http://localhost.invalid/v1", "http://127.1/v1",
            "http://2130706433/v1", "http://localhost.:80/v1", "http://localhost:0/v1",
            "http://localhost:65536/v1", "http://localhost:/v1", "http://[::1",
            " http://localhost/v1", "http://local\thost/v1", "http://localhost\\@external.invalid/v1",
        ):
            with self.subTest(endpoint=endpoint):
                with self.assertRaises(ModelClientError) as error:
                    validate_endpoint(endpoint)
                self.assertEqual(error.exception.code, "endpoint_not_allowed")

    def test_options_enforce_model_timeout_tokens_and_format(self):
        validate_options("test", 0.01, 1, "none")
        validate_options("test", 600, 4096, "json_schema")
        for options in (
            ("", 120, 4096, "none"), (" ", 120, 4096, "none"),
            ("test", 0, 4096, "none"), ("test", 601, 4096, "none"),
            ("test", float("nan"), 4096, "none"), ("test", float("inf"), 4096, "none"),
            ("test", True, 4096, "none"), ("test", 120, 0, "none"),
            ("test", 120, True, "none"), ("test", 120, 1.5, "none"),
            ("test", 120, 4096, "json_object"),
        ):
            with self.subTest(options=options):
                with self.assertRaises(ModelClientError):
                    validate_options(*options)


class ModelClientTransportTests(unittest.TestCase):
    def test_post_is_one_request_without_authorization_and_ignores_proxy_mapping(self):
        body = encode_and_validate_request(safe_request(), input_filename="synthetic.csv")
        with FakeModelServer(body={"choices": []}) as server:
            # Replace the mapping without enumerating or reading real values.
            with patch.object(os, "environ", {
                "HTTP_PROXY": "http://127.0.0.1:1", "HTTPS_PROXY": "http://127.0.0.1:1",
            }):
                result = post_request(validate_endpoint(server.endpoint), body, timeout=2)
            self.assertIsNone(result.error_code)
            self.assertEqual(result.status, 200)
            self.assertEqual(json.loads(result.body), {"choices": []})
            self.assertEqual(len(server.requests), 1)
            request = server.requests[0]
            self.assertEqual(request["path"], "/v1/chat/completions")
            self.assertEqual(request["body"], body)
            self.assertNotIn("authorization", {key.lower() for key in request["headers"]})
            self.assertTrue(result.complete)
            self.assertGreaterEqual(result.elapsed_ms, 0)

    def test_http_failure_preserves_response_body(self):
        with FakeModelServer(body=b"synthetic failure", status=500) as server:
            result = post_request(validate_endpoint(server.endpoint), b"{}", timeout=2)
            self.assertEqual((result.status, result.body, result.error_code),
                             (500, b"synthetic failure", "model_http_error"))
            self.assertEqual(len(server.requests), 1)

    def test_redirect_is_not_followed(self):
        with FakeModelServer(body=b"redirect", status=302, headers={"Location": "http://example.invalid"}) as server:
            result = post_request(validate_endpoint(server.endpoint), b"{}", timeout=2)
            self.assertEqual(result.error_code, "model_redirect_rejected")
            self.assertEqual(result.status, 302)
            self.assertEqual(result.body, b"redirect")
            self.assertEqual(len(server.requests), 1)

    def test_timeout_is_not_retried(self):
        with FakeModelServer(body=b"{}", delay=0.15) as server:
            result = post_request(validate_endpoint(server.endpoint), b"{}", timeout=0.03)
            self.assertEqual(result.error_code, "model_timeout")
            self.assertEqual(len(server.requests), 1)
            self.assertFalse(result.complete)

    def test_response_limit_stores_only_bounded_prefix(self):
        for length, error in ((MAX_RESPONSE_BYTES, None), (MAX_RESPONSE_BYTES + 1, "model_response_too_large")):
            with self.subTest(length=length):
                with FakeModelServer(body=b"x" * length) as server:
                    result = post_request(validate_endpoint(server.endpoint), b"{}", timeout=2)
                    self.assertEqual(result.error_code, error)
                    self.assertEqual(result.body, b"x" * MAX_RESPONSE_BYTES)
                    self.assertEqual(result.complete, error is None)
                    self.assertEqual(len(server.requests), 1)

    def test_connection_failure_is_controlled_without_environment_or_exception_details(self):
        # A bound socket that never listens guarantees an unused local endpoint.
        with socket.socket() as reserved:
            reserved.bind(("127.0.0.1", 0))
            port = reserved.getsockname()[1]
            result = post_request(validate_endpoint(f"http://127.0.0.1:{port}/v1"), b"{}", timeout=5)
        self.assertEqual(result.error_code, "model_connection_error")
        self.assertIsNone(result.body)
        self.assertIsNone(result.status)
        self.assertNotIn(str(port), result.message)


if __name__ == "__main__":
    unittest.main()
