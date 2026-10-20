from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_clean.csv_io import CSVError, Table, csv_bytes, read_csv
from researchops_clean.profile import (
    DATE_FORMATS_BY_PATTERN,
    SENTINEL_VALUES,
    ProfileError,
    build_profiles,
    format_pattern,
    profile_csv,
)


FIXTURE = Path(__file__).parent / "fixtures" / "lab_example.csv"


def table_for(values, name="检测"):
    rows = [{name: value} for value in values]
    raw = csv_bytes([name], rows)
    return Table([name], rows, hashlib.sha256(raw).hexdigest(), "synthetic.csv", "utf-8")


class ProfileTests(unittest.TestCase):
    def column(self, values, name="检测", k=5):
        return build_profiles(table_for(values, name), k=k)[1]["columns"][0]

    def test_existing_lab_fixture_classification_and_exact_counts(self):
        internal, model = profile_csv(FIXTURE)
        columns = {column["name"]: column for column in model["columns"]}
        self.assertEqual(columns["住院号"]["kind"], "identifier_like")
        self.assertEqual(columns["住院号"]["counts"]["leading_zero"], 11)
        self.assertEqual(columns["采样时间"]["kind"], "date_like")
        self.assertEqual(columns["采样时间"]["counts"]["excel_serial_candidate"], 1)
        self.assertEqual(columns["采样时间"]["counts"]["date_parseable"], 9)
        self.assertEqual(columns["结果"]["kind"], "numeric_with_tokens")
        self.assertEqual(columns["结果"]["counts"]["numeric_or_qualified"], 7)
        self.assertEqual(columns["结果"]["counts"]["numeric"], 5)
        self.assertEqual(columns["结果"]["numeric_ratio"], 5 / 11)
        self.assertEqual(columns["年龄"]["kind"], "age_text_like")
        self.assertEqual(columns["年龄"]["counts"]["age_text"], 10)
        self.assertEqual(model["input"]["row_count"], 11)
        self.assertEqual(internal["input"]["sha256"], hashlib.sha256(FIXTURE.read_bytes()).hexdigest())
        self.assertEqual(model["input"]["columns"], read_csv(FIXTURE).columns)

    def test_format_markers_preserve_separators(self):
        self.assertEqual(format_pattern("2023/5/6 8:30"), "9999/9/9 9:99")
        self.assertEqual(format_pattern("3岁2月"), "9汉9汉")
        self.assertEqual(format_pattern("Abz-１１０_中文🙂"), "AAA-999_汉汉🙂")

    def test_required_date_patterns_are_public(self):
        self.assertEqual(DATE_FORMATS_BY_PATTERN["9999/9/9 9:99"], ["%Y/%m/%d %H:%M"])
        self.assertEqual(DATE_FORMATS_BY_PATTERN["9999-99-99"], ["%Y-%m-%d"])
        self.assertEqual(DATE_FORMATS_BY_PATTERN["99999999"], ["%Y%m%d"])
        self.assertEqual(DATE_FORMATS_BY_PATTERN["9999/99/99"], ["%Y/%m/%d"])

    def test_sentinel_list_is_exact(self):
        self.assertEqual(
            SENTINEL_VALUES,
            ("999", "9999", "-1", "-99", "0000-00-00", "1900-01-01",
             "9999-12-31", "NA", "N/A", "NULL", "无", "不详", "未查"),
        )

    def test_empty_precedes_identifier_and_zero_row_table(self):
        for values in ([], ["", ""]):
            column = self.column(values, "patient_id")
            self.assertEqual(column["kind"], "empty")
            self.assertEqual(column["numeric_ratio"], 0)
            self.assertEqual(column["counts"]["distinct"], 0)
            self.assertEqual(column["patterns"], [])

    def test_identifier_english_words_and_camel_case(self):
        for name in (
            "ID", "record_id", "patientID", "userName", "subjectCode",
            "participant", "identifier", "email", "phone", "mobile", "address",
        ):
            with self.subTest(name=name):
                self.assertEqual(self.column(["1", "2", "3"], name)["kind"], "identifier_like")
        for name in ("lipid", "identityScore", "renamed", "telephoneScore"):
            with self.subTest(name=name):
                self.assertEqual(self.column(["1", "2", "3"], name)["kind"], "numeric")

    def test_identifier_chinese_keywords(self):
        for keyword in (
            "姓名", "名字", "身份证", "证件", "住院号", "门诊号", "病历号", "就诊卡",
            "医保", "电话", "手机", "联系方式", "地址", "住址", "监护人", "父亲",
            "母亲", "家长",
        ):
            with self.subTest(keyword=keyword):
                self.assertEqual(self.column(["3岁", "4岁"], keyword + "字段")["kind"], "identifier_like")

    def test_sensitive_format_precedes_numeric_and_code_kinds(self):
        for value in ("110101199001011234", "13800000000", "synthetic@example.test"):
            with self.subTest(value=value):
                column = self.column([value] * 5 + ["1"] * 20)
                self.assertEqual(column["kind"], "identifier_like")
                self.assertNotIn("values", column)
                self.assertNotIn("non_numeric_values", column)

    def test_age_requires_complete_unit_expression_and_strict_majority(self):
        accepted = ["3岁2月", "15天", "6岁", "1岁3个月", "5月12天", "2月", "3岁2个月1天"]
        column = self.column(accepted + ["999"])
        self.assertEqual(column["kind"], "age_text_like")
        self.assertEqual(column["counts"]["age_text"], len(accepted))
        rejected = ["岁", "3岁余", "年龄3岁", "1天2月", "３岁", "3.5岁", "3岁 "]
        self.assertEqual(self.column(rejected)["counts"]["age_text"], 0)
        self.assertNotEqual(self.column(["3岁", "未知"])["kind"], "age_text_like")

    def test_dates_require_successful_calendar_parsing(self):
        column = self.column(["2024-02-29", "2023/5/6 8:30", "20230507", "2023-02-29"])
        self.assertEqual(column["kind"], "date_like")
        self.assertEqual(column["counts"]["date_parseable"], 3)
        self.assertEqual(self.column(["2023-02-29", "2023-13-01"])["counts"]["date_parseable"], 0)
        self.assertNotEqual(self.column(["2023-05-01", "未知"])["kind"], "date_like")

    def test_common_date_widths_and_timestamps(self):
        values = [
            "2023-5-6", "2023/05/06", "2023.5.06", "2023-05-06T08:30:01",
            "2023/5/6 8:30:01", "2023年5月6日",
        ]
        self.assertEqual(self.column(values)["counts"]["date_parseable"], len(values))

    def test_excel_serial_requires_other_values_with_date_majority(self):
        with_dates = self.column(["45000", "2023-05-06", "2023-05-07", "未知"])
        self.assertEqual(with_dates["counts"]["excel_serial_candidate"], 1)
        exact_half = self.column(["45000", "2023-05-06", "未知"])
        self.assertEqual(exact_half["counts"]["excel_serial_candidate"], 0)
        all_numbers = self.column(["20000", "45000", "60000"])
        self.assertEqual(all_numbers["counts"]["excel_serial_candidate"], 0)
        self.assertEqual(all_numbers["kind"], "numeric")

    def test_excel_serial_bounds_and_integer_requirement(self):
        values = ["19999", "20000", "60000", "60001", "45000.5", "-45000"] + ["2023-05-06"] * 10
        self.assertEqual(self.column(values)["counts"]["excel_serial_candidate"], 2)

    def test_numeric_detection_matches_existing_operation(self):
        values = ["0", "-1", "+12", "001.20", "<0.5", ">=3", "≤-4", "≥+5", "１１０", "1e3", ".5"]
        column = self.column(values)
        self.assertEqual(column["kind"], "numeric_with_tokens")
        self.assertEqual(column["counts"]["numeric"], 4)
        self.assertEqual(column["counts"]["numeric_or_qualified"], 8)
        self.assertEqual(column["numeric_ratio"], 4 / 11)

    def test_numeric_majority_is_strict(self):
        self.assertEqual(self.column(["1", "阴性"])["kind"], "code_like")
        self.assertEqual(self.column(["1", "2", "阴性"])["kind"], "numeric_with_tokens")

    def test_repeated_integer_codes_require_all_three_conditions(self):
        self.assertEqual(self.column(["1", "2"] * 3)["kind"], "code_like")
        self.assertEqual(self.column(["1", "2", "1", "2", "1"])["kind"], "numeric")
        self.assertEqual(self.column(["1.0"] * 5)["kind"], "numeric")
        self.assertEqual(self.column([str(i) for i in range(11)] * 3)["kind"], "numeric")

    def test_short_codes_precede_high_uniqueness_fallback(self):
        self.assertEqual(self.column([f"v{i}" for i in range(10)])["kind"], "code_like")
        self.assertEqual(self.column([f"unique_record_{i:02d}" for i in range(10)])["kind"], "identifier_like")
        self.assertEqual(self.column([f"unique_record_{i:02d}" for i in range(9)])["kind"], "text")
        self.assertEqual(self.column(["a long repeated text"] * 10)["kind"], "text")

    def test_counts_distinguish_characters_values_and_union(self):
        column = self.column([" １２ ", "\u3000", "Ａ", "  ", ""])
        counts = column["counts"]
        self.assertEqual(counts["non_empty"], 4)
        self.assertEqual(counts["empty"], 1)
        self.assertEqual(counts["distinct"], 4)
        self.assertEqual(counts["fullwidth_characters"], 4)
        self.assertEqual(counts["fullwidth_values"], 3)
        self.assertEqual(counts["outer_whitespace_values"], 3)
        self.assertEqual(counts["normalization_affected"], 4)

    def test_leading_zeros_only_count_multidigit_ascii_integer_cells(self):
        self.assertEqual(
            self.column(["01", "00", "0", "+01", "01.2", "٠١", "００"])["counts"]["leading_zero"],
            2,
        )

    def test_sentinel_candidates_match_whole_cells_exactly(self):
        values = list(SENTINEL_VALUES) + ["999 ", "na", "无结果"]
        column = self.column(values)
        self.assertEqual(column["counts"]["sentinel_candidate"], 13)

    def test_patterns_sorted_by_frequency_then_text_and_capped_at_ten(self):
        values = ["b!"] * 4 + ["c?"] * 3 + ["x" + symbol for symbol in "#$%&()*+,-./"]
        internal, model = build_profiles(table_for(values))
        for profile in (internal, model):
            patterns = profile["columns"][0]["patterns"]
            self.assertEqual(len(patterns), 10)
            self.assertEqual(patterns[:2], [{"pattern": "A!", "count": 4}, {"pattern": "A?", "count": 3}])
            self.assertEqual(patterns, sorted(patterns, key=lambda item: (-item["count"], item["pattern"])))

    def test_duplicate_counts_include_all_group_members_without_keys(self):
        rows = [{"first": "x", "second": "1"}] * 3 + [{"first": "y", "second": "2"}] * 2 + [
            {"first": "x", "second": "2"}
        ]
        table = Table(["first", "second"], rows, "0" * 64, "synthetic.csv", "utf-8")
        internal, model = build_profiles(table)
        for profile in (internal, model):
            self.assertEqual(profile["row_summary"], {"duplicate_row_count": 5, "duplicate_group_count": 2})
            self.assertNotIn("rows", profile)
            self.assertNotIn("keys", profile["row_summary"])

    def test_k_rejects_small_non_integer_and_boolean_values(self):
        for k in (4, 0, -1, "5", 5.0, True, None):
            with self.subTest(k=k):
                with self.assertRaises(ProfileError) as caught:
                    build_profiles(table_for(["男"] * 5), k=k)
                self.assertEqual(caught.exception.code, "profile_invalid_k")

    def test_configured_k_is_applied_to_values_and_other_counts(self):
        column = self.column(["男"] * 6 + ["女"] * 5 + ["未知"], k=6)
        self.assertEqual(column["values"], [{"value": "男", "count": 6}])
        self.assertEqual(column["other_values"], {"distinct": 2, "count": 6})

    def test_profiles_reuse_shared_csv_reader_and_encoding(self):
        table = table_for(["1"])
        with patch("researchops_clean.profile.csv_io.read_csv", return_value=table) as reader:
            profile_csv("synthetic.csv", encoding="gb18030")
        reader.assert_called_once_with(Path("synthetic.csv"), encoding="gb18030")

    def test_invalid_csv_errors_remain_path_free(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaises(CSVError) as caught:
                profile_csv(Path(temporary) / "missing.csv")
            self.assertEqual(caught.exception.code, "input_read_error")
            self.assertNotIn(temporary, str(caught.exception))

    def test_profile_objects_and_serialization_are_deterministic(self):
        original = read_csv(FIXTURE)
        rows_before = [row.copy() for row in original.rows]
        outputs = [
            json.dumps(build_profiles(original), ensure_ascii=False, sort_keys=True)
            for _ in range(2)
        ]
        self.assertEqual(outputs[0], outputs[1])
        self.assertEqual(original.rows, rows_before)


if __name__ == "__main__":
    unittest.main()
