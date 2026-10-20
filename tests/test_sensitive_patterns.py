from __future__ import annotations

import unittest

from researchops_clean.sensitive_patterns import _PATTERNS


class SensitivePatternTests(unittest.TestCase):
    def test_only_the_five_requested_categories_are_present(self):
        self.assertEqual(
            set(_PATTERNS),
            {"identity_number", "phone_number", "email", "file_path", "api_key"},
        )

    def test_synthetic_sensitive_values_match_their_categories(self):
        samples = {
            "identity_number": "11010520000101001X",
            "phone_number": "19900000000",
            "email": "synthetic@example.invalid",
            "file_path": r"Z:\synthetic\sample.csv",
            "api_key": "sk-synthetic-example-only",
        }
        for category, text in samples.items():
            with self.subTest(category=category):
                self.assertIsNotNone(_PATTERNS[category].search(text))

    def test_ordinary_chinese_and_measurements_do_not_match(self):
        samples = (
            "这是一句普通中文，仅用于合成数据测试。",
            "血红蛋白为 112 g/L。",
            "112 g/L",
            "5.6 mmol/L",
            "治疗组/对照组",
            "收缩压/舒张压",
            "2026/10/13",
            "mL/min/1.73m²",
        )
        for text in samples:
            for category, pattern in _PATTERNS.items():
                with self.subTest(text=text, category=category):
                    self.assertIsNone(pattern.search(text))

    def test_synthetic_relative_and_network_paths_match(self):
        for text in (
            "artifacts/synthetic/sample.csv",
            "./synthetic.csv",
            "../synthetic/sample.csv",
            r"\\synthetic-server\synthetic-share\sample.csv",
            r"\\合成服务器\合成共享\sample.csv",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(_PATTERNS["file_path"].search(text))

    def test_subject_identifier_is_not_a_retained_rule(self):
        for category, pattern in _PATTERNS.items():
            with self.subTest(category=category):
                self.assertIsNone(pattern.search("SUBJ-SYNTHETIC"))
