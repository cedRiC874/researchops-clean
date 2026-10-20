from __future__ import annotations

import json
import tempfile
import unittest
from collections import Counter
from pathlib import Path

from researchops_clean.csv_io import Table, read_csv
from researchops_clean.profile import ProfileError, build_profiles, profile_csv
from researchops_clean.sensitive_patterns import find_sensitive_types


FIXTURE = Path(__file__).parent / "fixtures" / "profile_privacy.csv"


def strings_in(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings_in(item)


class ProfilePrivacyTests(unittest.TestCase):
    def setUp(self):
        self.internal, self.model = profile_csv(FIXTURE)
        self.serialized = json.dumps(self.model, ensure_ascii=False, sort_keys=True)

    def test_synthetic_identifiers_and_rare_values_are_absent(self):
        for value in (
            "110101199001011234", "13800000000", "synthetic@example.test",
            "罕见紫鹭", "罕见橙鹭", "sk-synthetic-only",
            "C:\\synthetic\\private.csv", "/synthetic/private.csv",
        ):
            with self.subTest(value=value):
                self.assertNotIn(value, self.serialized)

    def test_no_value_or_pattern_matches_sensitive_detection(self):
        for text in strings_in(self.model):
            with self.subTest(text=text):
                self.assertEqual(find_sensitive_types(text), [])

    def test_all_disclosed_values_are_allowed_kinds_and_meet_k(self):
        table = read_csv(FIXTURE)
        for column in self.model["columns"]:
            actual = Counter(row[column["name"]] for row in table.rows if row[column["name"]])
            for key in ("values", "non_numeric_values"):
                if key not in column:
                    continue
                expected_kind = "code_like" if key == "values" else "numeric_with_tokens"
                self.assertEqual(column["kind"], expected_kind)
                for item in column[key]:
                    self.assertGreaterEqual(item["count"], self.model["privacy"]["k"])
                    self.assertEqual(item["count"], actual[item["value"]])
            if column["kind"] == "identifier_like":
                self.assertNotIn("values", column)
                self.assertNotIn("non_numeric_values", column)

    def test_other_counts_describe_hidden_values_without_names(self):
        columns = {column["name"]: column for column in self.model["columns"]}
        self.assertEqual(columns["分组"]["other_values"], {"distinct": 1, "count": 1})
        self.assertEqual(columns["检测"]["non_numeric_values"], [{"value": "阴性", "count": 5}])
        self.assertEqual(columns["检测"]["other_values"], {"distinct": 1, "count": 1})
        self.assertNotIn("values", columns["路径资料"])
        self.assertEqual(columns["路径资料"]["other_values"], {"distinct": 0, "count": 0})

    def test_pattern_strings_cannot_reveal_unchanged_rare_values(self):
        patterns = {item["pattern"] for column in self.model["columns"] for item in column["patterns"]}
        for value in ("🙂", "@@@", "999999999999999999", "99/99", "🙂🙂", "AA@AA.AA", "++", "999", "汉"):
            with self.subTest(value=value):
                self.assertNotIn(value, patterns)
        self.assertIn("[已隐藏模式]", patterns)

    def test_identifier_columns_never_disclose_values_even_above_k(self):
        columns = {column["name"]: column for column in self.model["columns"]}
        for name in ("记录", "联系", "通信", "模式边界"):
            with self.subTest(name=name):
                self.assertEqual(columns[name]["kind"], "identifier_like")
                self.assertNotIn("values", columns[name])
                self.assertNotIn("non_numeric_values", columns[name])

    def test_numeric_and_age_sentinels_are_only_aggregate_counts(self):
        rows = [{"年龄": value} for value in ["3岁"] * 10 + ["999"]]
        table = Table(["年龄"], rows, "0" * 64, "synthetic.csv", "utf-8")
        internal, model = build_profiles(table)
        column = model["columns"][0]
        self.assertEqual(column["counts"]["sentinel_candidate"], 1)
        self.assertNotIn("sentinel_values", column)
        self.assertNotIn("values", column)
        self.assertNotIn("non_numeric_values", column)
        self.assertNotIn("999", list(strings_in(model)))
        self.assertIn({"value": "999", "count": 1}, internal["columns"][0]["sentinel_values"])

    def test_source_directory_and_filename_are_not_serialized(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic_private_source.csv"
            path.write_bytes(FIXTURE.read_bytes())
            outputs = profile_csv(path)
            for output in outputs:
                serialized = json.dumps(output, ensure_ascii=False)
                self.assertNotIn(temporary, serialized)
                self.assertNotIn(path.name, serialized)
                self.assertNotIn("filename", output["input"])
                self.assertNotIn("path", output["input"])

    def test_internal_values_keep_local_details_but_mask_paths_and_credentials(self):
        serialized = json.dumps(self.internal, ensure_ascii=False)
        self.assertIn("110101199001011234", serialized)
        self.assertIn("罕见紫鹭", serialized)
        self.assertNotIn("sk-synthetic-only", serialized)
        for text in strings_in(self.internal):
            self.assertNotIn("file_path", find_sensitive_types(text))
            self.assertNotIn("api_key", find_sensitive_types(text))

    def test_sensitive_or_path_headers_are_rejected_without_echoing_header(self):
        for name in (
            "13800000000", "110101199001011234", "person@example.test",
            "C:\\synthetic\\data.csv", "/患者/数据.csv", "sk-synthetic-only",
        ):
            with self.subTest(name=name):
                table = Table([name], [{name: "男"}], "0" * 64, "synthetic.csv", "utf-8")
                with self.assertRaises(ProfileError) as caught:
                    build_profiles(table)
                self.assertEqual(caught.exception.code, "profile_header_sensitive")
                self.assertNotIn(name, str(caught.exception))

    def test_chinese_absolute_paths_are_hidden_as_values_and_patterns(self):
        values = ["/患者/数据.csv"] * 5 + ["正常"] * 5
        table = Table(["资料"], [{"资料": value} for value in values], "0" * 64, "synthetic.csv", "utf-8")
        internal, model = build_profiles(table)
        self.assertEqual(model["columns"][0]["values"], [{"value": "正常", "count": 5}])
        for profile in (internal, model):
            serialized = json.dumps(profile, ensure_ascii=False)
            self.assertNotIn("/患者/数据.csv", serialized)
            self.assertNotIn("/汉汉/汉汉.AAA", serialized)

    def test_short_paths_in_code_columns_only_contribute_hidden_counts(self):
        rows = [{"资料": value} for value in ["/a/x"] * 5 + ["正常"] * 5]
        table = Table(["资料"], rows, "0" * 64, "synthetic.csv", "utf-8")
        _, model = build_profiles(table)
        column = model["columns"][0]
        self.assertEqual(column["kind"], "code_like")
        self.assertEqual(column["values"], [{"value": "正常", "count": 5}])
        self.assertEqual(column["other_values"], {"distinct": 1, "count": 5})

    def test_embedded_single_segment_paths_and_fullwidth_secrets_are_hidden(self):
        secrets = [
            "see /private", "note (/private)", "see Ｃ：＼private",
            "Ｃ：＼资料", "ｓｋ－synthetic", "／private",
        ]
        for secret in secrets:
            with self.subTest(secret=secret):
                rows = [{"资料": secret}] * 5
                table = Table(["资料"], rows, "0" * 64, "synthetic.csv", "utf-8")
                internal, model = build_profiles(table)
                self.assertNotIn(secret, list(strings_in(model)))
                self.assertNotIn(secret, list(strings_in(internal)))
                self.assertEqual(model["columns"][0].get("values", []), [])

    def test_single_segment_paths_after_punctuation_are_hidden(self):
        for secret in ("路径：/秘密", "位置:/private", "提示，/secret"):
            with self.subTest(secret=secret):
                rows = [{"备注": secret}] * 5
                table = Table(["备注"], rows, "0" * 64, "synthetic.csv", "utf-8")
                internal, model = build_profiles(table)
                for profile in (internal, model):
                    self.assertNotIn(secret, list(strings_in(profile)))
                    self.assertNotIn("汉汉：/汉汉", list(strings_in(profile)))
                self.assertEqual(model["columns"][0].get("values", []), [])
        rows = [{"日期": "2023/5/6 8:30"}] * 5
        table = Table(["日期"], rows, "0" * 64, "synthetic.csv", "utf-8")
        _, model = build_profiles(table)
        self.assertEqual(model["columns"][0]["kind"], "date_like")
        self.assertEqual(model["columns"][0]["patterns"], [{"pattern": "9999/9/9 9:99", "count": 5}])

    def test_unit_slashes_are_not_filtered_as_paths(self):
        for unit in ("g/L", "mL/min/1.73m²", "112 g/L"):
            with self.subTest(unit=unit):
                rows = [{"单位": unit}] * 5
                table = Table(["单位"], rows, "0" * 64, "synthetic.csv", "utf-8")
                _, model = build_profiles(table)
                if len(unit) <= 10:
                    self.assertEqual(model["columns"][0]["values"], [{"value": unit, "count": 5}])
                else:
                    self.assertEqual(model["columns"][0]["other_values"], {"distinct": 0, "count": 0})

    def test_model_has_no_extrema_quantiles_or_row_data(self):
        forbidden = {"min", "max", "minimum", "maximum", "quantiles", "median", "rows", "records", "samples"}
        for key in strings_in(self.model):
            self.assertNotIn(key, forbidden)


if __name__ == "__main__":
    unittest.main()
