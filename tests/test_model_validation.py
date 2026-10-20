"""Deterministic proposal checks over synthetic, privacy-filtered profiles."""
from __future__ import annotations

import copy
import tempfile
import unittest
from pathlib import Path

from researchops_clean.csv_io import CSVError
from researchops_clean.issues import (
    _INVALID_VALUES, _QUALITATIVE_VALUES, propose_from_profile,
)
from researchops_clean.model_proposer import ModelOutputError, validate_proposals
from researchops_clean.ops import declared_output_columns
from researchops_clean.profile import SENTINEL_VALUES, profile_csv


FIXTURES = Path(__file__).parent / "fixtures"


def mixed_proposals(model_profile, rules):
    """The eleven required cases, in a stable human-auditable order."""
    existing = rules["steps"][0]
    new_column = next(
        name for step in rules["steps"]
        for name in declared_output_columns(step["op"], step["params"])
    )
    return [
        {"op": "map_values", "params": {
            "column": "性别", "mapping": {"M": "男"}, "on_unmapped": "keep_and_flag",
        }, "reason": "统一可见代码", "issue_id": "I1"},
        {"op": "sentinel_to_missing", "params": {
            "column": "代码", "values": ["NA"],
        }, "reason": "请人工核对固定代码"},
        {"op": existing["op"], "params": copy.deepcopy(existing["params"]),
         "reason": "重复规则建议"},
        {"op": "normalize_text", "params": {"column": new_column},
         "reason": "规范规则新增列"},
        {"op": "invented_operation", "params": {}, "reason": "未知操作"},
        {"op": "range_check", "params": {"column": "代码", "min": "0", "max": "9"},
         "reason": "模型不能给医学界限"},
        {"op": "map_values", "params": {
            "column": "性别", "mapping": {"M": ""}, "on_unmapped": "keep",
        }, "reason": "空目标无效"},
        {"op": "normalize_text", "params": {"column": "不存在的列"}, "reason": "未知列"},
        {"op": "map_values", "params": {
            "column": "性别", "mapping": {"编造取值": "男"}, "on_unmapped": "keep",
        }, "reason": "不得猜测取值"},
        {"op": "normalize_text", "params": {"column": "性别"},
         "reason": "合成手机号 13800000000"},
        {"op": "normalize_text", "params": {"column": "性别"}, "reason": "长" * 201},
    ]


class ModelValidationTests(unittest.TestCase):
    def setUp(self):
        _, self.profile = profile_csv(FIXTURES / "profile_workflow.csv")
        self.issues, self.rules = propose_from_profile(self.profile)

    def validate(self, proposals):
        return validate_proposals(
            {"proposals": proposals}, self.profile, self.issues, self.rules,
        )

    def test_eleven_mixed_cases_keep_order_and_pending_contract(self):
        original = copy.deepcopy((self.profile, self.issues, self.rules))
        report, draft = self.validate(mixed_proposals(self.profile, self.rules))
        self.assertEqual([item["index"] for item in report["accepted"]], [1, 2, 4])
        self.assertEqual([item["step"]["step_id"] for item in report["accepted"]],
                         ["M1", "M2", "M3"])
        self.assertEqual(report["accepted"][0]["issue_id"], "I1")
        self.assertEqual(
            [warning["code"] for warning in report["accepted"][1]["warnings"]],
            ["value_unverified"],
        )
        self.assertEqual(report["accepted"][0]["warnings"], [])
        self.assertEqual([(item["index"], item["error_code"]) for item in report["rejected"]], [
            (5, "op_not_allowed"), (6, "op_not_allowed"), (7, "params_invalid"),
            (8, "column_unknown"), (9, "value_not_in_profile"),
            (10, "reason_invalid"), (11, "reason_invalid"),
        ])
        self.assertEqual(len(report["duplicates"]), 1)
        self.assertEqual(report["duplicates"][0]["index"], 3)
        self.assertEqual(report["duplicates"][0]["duplicate_of"], self.rules["steps"][0]["step_id"])
        self.assertEqual(draft["steps"][:len(self.rules["steps"])], self.rules["steps"])
        for step in draft["steps"]:
            self.assertEqual(step["approval"], {"status": "pending"})
        for step in draft["steps"][len(self.rules["steps"]):]:
            self.assertTrue(step["reason"].startswith("【模型建议】"))
        self.assertEqual((self.profile, self.issues, self.rules), original)

    def test_whole_envelope_schema_rejects_extra_missing_and_over_limit(self):
        valid = {"op": "normalize_text", "params": {"column": "性别"}, "reason": "合成建议"}
        invalid = [
            [], {}, {"proposals": {}}, {"proposals": [], "extra": True},
            {"proposals": [dict(valid, extra="不允许")]},
            {"proposals": [{key: value for key, value in valid.items() if key != "reason"}]},
            {"proposals": [dict(valid, params=[])]},
            {"proposals": [dict(valid, reason=1)]},
            {"proposals": [dict(valid, issue_id=1)]},
            {"proposals": [valid] * 31},
        ]
        for envelope in invalid:
            with self.subTest(envelope=envelope):
                with self.assertRaises(ModelOutputError) as caught:
                    validate_proposals(envelope, self.profile, self.issues, self.rules)
                self.assertEqual(caught.exception.code, "model_output_invalid")

    def test_empty_and_thirty_proposals_are_valid_envelopes(self):
        report, draft = self.validate([])
        self.assertEqual(report, {"accepted": [], "rejected": [], "duplicates": []})
        self.assertEqual(draft, self.rules)
        proposal = {"op": "normalize_text", "params": {"column": "性别"}, "reason": "统一文本"}
        report, _ = self.validate([proposal] * 30)
        self.assertEqual(len(report["accepted"]), 1)
        self.assertEqual(len(report["duplicates"]), 29)

    def test_nonfinite_values_and_unpaired_surrogates_are_whole_output_errors(self):
        for value in (float("inf"), float("-inf"), float("nan")):
            with self.subTest(value=value):
                with self.assertRaises(ModelOutputError) as caught:
                    self.validate([{
                        "op": "normalize_text",
                        "params": {"column": "性别", "unexpected": value},
                        "reason": "合成建议",
                    }])
                self.assertEqual(caught.exception.code, "model_output_invalid")
        with self.assertRaises(ModelOutputError) as caught:
            self.validate([{
                "op": "normalize_text", "params": {"column": "性别"}, "reason": "\ud800",
            }])
        self.assertEqual(caught.exception.code, "model_output_invalid")

    def test_new_model_columns_accumulate_and_duplicate_accepted_is_not_appended(self):
        proposal = {"op": "parse_age_cn", "params": {
            "column": "年龄", "outputs": {
                "years": "模型年", "months": "模型月", "days": "模型天",
            },
        }, "reason": "拆分年龄"}
        report, draft = self.validate([
            proposal,
            {"op": "normalize_text", "params": {"column": "模型年"}, "reason": "规范新增列"},
            copy.deepcopy(proposal),
        ])
        self.assertEqual([item["index"] for item in report["accepted"]], [1, 2])
        self.assertEqual(report["rejected"], [])
        self.assertEqual(report["duplicates"][0]["duplicate_of"], "M1")
        self.assertEqual(len(draft["steps"]), len(self.rules["steps"]) + 2)

    def test_nfc_equivalent_columns_are_rejected_before_proposals(self):
        composed, decomposed = "\u00e9", "e\u0301"
        self.assertNotEqual(composed, decomposed)
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "synthetic.csv"
            for first_value in ("A", " A "):
                with self.subTest(first_value=first_value):
                    source.write_text(
                        f"{composed},{decomposed}\n" + f"{first_value},B\n" * 5,
                        encoding="utf-8", newline="\n",
                    )
                    with self.assertRaises(CSVError) as raised:
                        profile_csv(source)
                    self.assertEqual(raised.exception.code, "ambiguous_column_names")

    def test_output_conflicts_and_rejected_steps_do_not_create_columns(self):
        proposals = [
            {"op": "parse_age_cn", "params": {
                "column": "年龄", "outputs": {
                    "years": "性别", "months": "冲突月", "days": "冲突天",
                },
            }, "reason": "不能覆盖输入"},
            {"op": "normalize_text", "params": {"column": "冲突月"}, "reason": "此前被拒绝"},
            {"op": "parse_age_cn", "params": {
                "column": "年龄", "outputs": {"years": "同名", "months": "同名", "days": "另列"},
            }, "reason": "输出列不能相同"},
        ]
        report, _ = self.validate(proposals)
        self.assertEqual([item["error_code"] for item in report["rejected"]],
                         ["output_column_conflict", "column_unknown", "output_column_conflict"])

    def test_reason_empty_whitespace_sensitive_and_exact_limit(self):
        for reason in ("", " \n\t", "假证件 110101199001011234", "13800000000",
                       "synthetic@example.test", "sk-synthetic-only", r"C:\synthetic\file.csv"):
            with self.subTest(reason=reason):
                report, _ = self.validate([{
                    "op": "normalize_text", "params": {"column": "性别"}, "reason": reason,
                }])
                self.assertEqual(report["rejected"][0]["error_code"], "reason_invalid")
        report, _ = self.validate([{
            "op": "normalize_text", "params": {"column": "性别"}, "reason": "合" * 200,
        }])
        self.assertEqual(len(report["accepted"]), 1)

    def test_map_targets_may_be_new_nonempty_strings_but_sources_are_column_scoped(self):
        report, _ = self.validate([
            {"op": "map_values", "params": {
                "column": "性别", "mapping": {"M": "全新规范标签"}, "on_unmapped": "keep",
            }, "reason": "目标不要求预先可见"},
            {"op": "map_values", "params": {
                "column": "性别", "mapping": {"A": "男"}, "on_unmapped": "keep",
            }, "reason": "其他列可见不代表本列可见"},
            {"op": "map_values", "params": {
                "column": "性别", "mapping": {}, "on_unmapped": "keep",
            }, "reason": "空映射拒绝"},
        ])
        self.assertEqual(len(report["accepted"]), 1)
        self.assertEqual([item["error_code"] for item in report["rejected"]],
                         ["value_not_in_profile", "params_invalid"])

    def test_existing_fixed_vocabularies_are_reused_with_unverified_warning(self):
        vocabulary = list(dict.fromkeys(SENTINEL_VALUES + _QUALITATIVE_VALUES + _INVALID_VALUES))
        report, _ = self.validate([{
            "op": "sentinel_to_missing", "params": {"column": "性别", "values": vocabulary},
            "reason": "所有固定词均需本机核对",
        }])
        self.assertEqual(report["rejected"], [])
        self.assertTrue(report["accepted"][0]["warnings"])
        self.assertEqual({item["code"] for item in report["accepted"][0]["warnings"]},
                         {"value_unverified"})

    def test_parse_lab_values_check_source_column_and_shared_strict_semantics(self):
        def proposal(qualitative, invalid):
            return {"op": "parse_lab_result", "params": {
                "column": "结果", "qualitative_values": qualitative, "invalid_values": invalid,
                "outputs": {"value": "模型数值", "qualifier": "模型符号",
                            "qualitative": "模型定性", "invalid": "模型无效"},
            }, "reason": "解析结果"}
        for candidate, error in (
            (proposal(["M"], []), "value_not_in_profile"),
            (proposal([], ["编造无效词"]), "value_not_in_profile"),
            (proposal(["阴性"], ["阴性"]), "params_invalid"),
        ):
            with self.subTest(error=error):
                report, _ = self.validate([candidate])
                self.assertEqual(report["rejected"][0]["error_code"], error)
        report, _ = self.validate([proposal(["阴性"], ["凝块"])])
        self.assertEqual(len(report["accepted"]), 1)
        self.assertEqual(report["accepted"][0]["warnings"][0]["code"], "value_unverified")

    def test_conversion_units_must_be_visible_in_unit_column(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "synthetic.csv"
            text = "数值,单位,其他\n" + "1,mg,hidden-unit\n" * 5 + "2,g,other-unit\n" * 5
            path.write_text(text, encoding="utf-8", newline="\n")
            _, model = profile_csv(path)
            issues, rules = propose_from_profile(model)
        def validate(source, target, **extra):
            return validate_proposals({"proposals": [{
                "op": "convert_unit", "params": {
                    "value_column": "数值", "unit_column": "单位",
                    "conversions": [{"from": source, "to": target, "factor": "0.001"}], **extra,
                }, "reason": "只使用可见单位",
            }]}, model, issues, rules)[0]
        self.assertEqual(len(validate("mg", "g")["accepted"]), 1)
        for source, target in (("mg", "unknown"), ("hidden-unit", "g"), ("NA", "g")):
            with self.subTest(source=source, target=target):
                report = validate(source, target)
                if source == "NA":
                    self.assertEqual(report["accepted"][0]["warnings"][0]["code"], "value_unverified")
                else:
                    self.assertEqual(report["rejected"][0]["error_code"], "value_not_in_profile")
        self.assertEqual(validate("mg", "g", where={"column": "未知", "equals": "A"})
                         ["rejected"][0]["error_code"], "column_unknown")


if __name__ == "__main__":
    unittest.main()
