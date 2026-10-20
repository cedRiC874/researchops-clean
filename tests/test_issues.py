from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_clean.issues import IssueError, propose_from_profile
from researchops_clean.profile import profile_csv
from researchops_clean.recipe import validate_recipe, validate_steps


def column(name, kind="text", **overrides):
    result = {
        "name": name, "kind": kind,
        "counts": {
            "non_empty": 20, "empty": 0, "distinct": 2, "numeric": 0,
            "numeric_or_qualified": 0, "leading_zero": 0,
            "excel_serial_candidate": 0, "sentinel_candidate": 0,
            "fullwidth_characters": 0, "fullwidth_values": 0,
            "outer_whitespace_values": 0, "normalization_affected": 0,
            "age_text": 0, "date_parseable": 0,
        },
        "numeric_ratio": 0,
        "patterns": [],
        "other_values": {"distinct": 0, "count": 0},
    }
    if kind == "code_like":
        result["values"] = []
    if kind == "numeric_with_tokens":
        result["non_numeric_values"] = []
    for key, value in overrides.items():
        if key == "counts":
            result["counts"].update(value)
        else:
            result[key] = value
    return result


def model(*columns, duplicates=0):
    return {
        "schema_version": "cleaning-profile/model/1",
        "input": {"sha256": "a" * 64, "columns": [item["name"] for item in columns], "row_count": 20},
        "privacy": {"k": 5},
        "row_summary": {"duplicate_row_count": duplicates, "duplicate_group_count": 1 if duplicates else 0},
        "columns": list(columns),
    }


def suggestions(profile, op):
    issues, recipe = propose_from_profile(profile)
    return [step for step in recipe["steps"] if step["op"] == op], issues["issues"], recipe


class IssueTests(unittest.TestCase):
    def test_all_rules_have_resolvable_evidence_and_pending_steps(self):
        source = model(
            column("编号", "identifier_like", counts={"leading_zero": 20}),
            column("性别", "code_like", values=[
                {"value": value, "count": 5} for value in ("男", "M", "女", "F")
            ]),
            column("备注", counts={"normalization_affected": 6, "fullwidth_characters": 5, "outer_whitespace_values": 4}),
            column("缺失代码", "code_like", values=[
                {"value": "999", "count": 10}, {"value": "正常", "count": 10}
            ], counts={"sentinel_candidate": 10}),
            column("日期", "date_like", patterns=[
                {"pattern": "9999/9/9 9:99", "count": 5},
                {"pattern": "9999-99-99", "count": 5},
                {"pattern": "99999999", "count": 5},
                {"pattern": "99999", "count": 5},
            ], counts={"date_parseable": 15, "excel_serial_candidate": 5}),
            column("年龄", "age_text_like", counts={"age_text": 20}),
            column("结果", "numeric_with_tokens", non_numeric_values=[
                {"value": "阴性", "count": 5}, {"value": "未知合成词", "count": 5}
            ], counts={"numeric": 10, "numeric_or_qualified": 10}, numeric_ratio=0.5),
            duplicates=4,
        )
        original = copy.deepcopy(source)
        issues, recipe = propose_from_profile(source)
        self.assertEqual(source, original)
        self.assertEqual({step["op"] for step in recipe["steps"]}, {
            "flag_duplicates", "normalize_text", "sentinel_to_missing",
            "parse_date", "parse_age_cn", "parse_lab_result", "map_values",
        })
        self.assertEqual(recipe["input"], {"sha256": source["input"]["sha256"], "columns": source["input"]["columns"]})
        self.assertEqual([step["step_id"] for step in recipe["steps"]], [f"S{i + 1}" for i in range(7)])
        self.assertTrue(all(step["approval"] == {"status": "pending"} for step in recipe["steps"]))
        self.assertEqual({card["category"] for card in issues["issues"]}, {"符合性", "完整性", "合理性"})
        for card in issues["issues"]:
            for item in card["evidence"]:
                value = source
                for token in item["pointer"].lstrip("/").split("/"):
                    token = token.replace("~1", "/").replace("~0", "~")
                    value = value[int(token)] if isinstance(value, list) else value[token]
                self.assertEqual(value, item["value"])
        validate_recipe(recipe)
        for step in recipe["steps"]:
            if step["op"] == "map_values":
                step["params"]["mapping"] = {key: key for key in step["params"]["mapping"]}
        validate_steps(recipe, require_approved=False)

    def test_only_supplied_public_object_is_read(self):
        source = model(column("未知缺失列", counts={"sentinel_candidate": 3}))
        with patch("builtins.open", side_effect=AssertionError("must not open local files")), patch(
            "pathlib.Path.read_text", side_effect=AssertionError("must not read internal profile")
        ), patch("pathlib.Path.read_bytes", side_effect=AssertionError("must not read CSV")):
            issues, recipe = propose_from_profile(source)
        params = recipe["steps"][0]["params"]
        self.assertNotIn("values", params)
        self.assertIn("本机", " ".join(issues["issues"][0]["needs_human"]))

    def test_identifier_has_card_and_no_operations_even_when_other_signals_exist(self):
        source = model(column("住院号", "identifier_like", counts={
            "leading_zero": 20, "normalization_affected": 6,
            "sentinel_candidate": 5, "excel_serial_candidate": 5,
        }))
        issues, recipe = propose_from_profile(source)
        self.assertEqual(recipe["steps"], [])
        self.assertEqual(len(issues["issues"]), 1)
        self.assertIn("将在去标识化阶段处理", issues["issues"][0]["needs_human"][0])

    def test_sentinel_uses_only_visible_candidates_not_entire_static_vocabulary(self):
        source = model(column("状态", "code_like", counts={"sentinel_candidate": 7}, values=[
            {"value": "NA", "count": 5}, {"value": "正常", "count": 13},
        ], other_values={"distinct": 2, "count": 2}))
        steps, cards, _ = suggestions(source, "sentinel_to_missing")
        self.assertEqual(steps[0]["params"]["values"], ["NA"])
        self.assertTrue(any("未公开" in text for text in cards[0]["needs_human"]))

    def test_normalization_affected_count_does_not_double_count_overlapping_signals(self):
        source = model(column("备注", counts={
            "normalization_affected": 6, "fullwidth_values": 4,
            "fullwidth_characters": 9, "outer_whitespace_values": 4,
        }))
        steps, cards, _ = suggestions(source, "normalize_text")
        self.assertEqual(steps[0]["params"], {"column": "备注"})
        self.assertEqual(cards[0]["affected_rows"], 6)

    def test_dates_derive_known_formats_and_excel_options_from_visible_fields(self):
        source = model(column("时间", "date_like", patterns=[
            {"pattern": "9999/9/9 9:99", "count": 5},
            {"pattern": "9999-99-99", "count": 5},
            {"pattern": "99999999", "count": 5},
            {"pattern": "9999/99/99", "count": 5},
        ], counts={"date_parseable": 20}))
        steps, _, _ = suggestions(source, "parse_date")
        self.assertEqual(set(steps[0]["params"]["formats"]), {"%Y/%m/%d %H:%M", "%Y-%m-%d", "%Y%m%d", "%Y/%m/%d"})
        self.assertEqual(steps[0]["params"]["output"], "datetime")
        self.assertFalse(steps[0]["params"]["excel_serial"]["enabled"])
        serial_only = model(column("时间", "date_like", counts={"excel_serial_candidate": 5}))
        steps, cards, _ = suggestions(serial_only, "parse_date")
        self.assertTrue(steps[0]["params"]["excel_serial"]["enabled"])
        self.assertNotIn("formats", steps[0]["params"])
        self.assertTrue(any("formats" in text for text in cards[0]["needs_human"]))

    def test_one_date_format_and_plain_numeric_column_need_no_steps(self):
        source = model(
            column("时间", "date_like", patterns=[{"pattern": "9999-99-99", "count": 20}], counts={"date_parseable": 20}),
            column("数值", "numeric", counts={"numeric": 20}, numeric_ratio=1),
        )
        self.assertEqual(propose_from_profile(source)[1]["steps"], [])

    def test_lab_unknown_visible_tokens_and_hidden_aggregate_require_review(self):
        source = model(column("结果", "numeric_with_tokens", non_numeric_values=[
            {"value": "<0.5", "count": 5}, {"value": "溶血", "count": 5},
            {"value": "未收录合成词", "count": 5},
        ], other_values={"distinct": 1, "count": 1}))
        steps, cards, _ = suggestions(source, "parse_lab_result")
        self.assertEqual(steps[0]["params"]["qualitative_values"], ["阴性", "阳性", "+", "++", "+++", "弱阳性"])
        self.assertEqual(steps[0]["params"]["invalid_values"], ["溶血", "标本不合格", "脂血", "凝块"])
        review = " ".join(cards[0]["needs_human"])
        self.assertIn("未收录合成词", review)
        self.assertIn("未公开", review)
        self.assertNotIn("“<0.5”", review)
        self.assertNotIn("“溶血”", review)

    def test_map_requires_supported_variants_and_contains_all_visible_keys(self):
        for values in (("男", "男性", "女"), ("HGB", "Hb", "CRP"), ("ＡＢ", "AB", "Other"), ("yes", "YES", "NO")):
            with self.subTest(values=values):
                source = model(column("代码", "code_like", values=[
                    {"value": value, "count": 5} for value in values
                ], other_values={"distinct": 1, "count": 1}))
                steps, cards, _ = suggestions(source, "map_values")
                self.assertEqual(steps[0]["params"]["mapping"], dict.fromkeys(values, ""))
                self.assertEqual(steps[0]["params"]["on_unmapped"], "keep_and_flag")
                review = " ".join(cards[0]["needs_human"])
                self.assertIn("男→男", review)
                self.assertIn("补充", review)
        for values in (("男", "女"), ("CRP", "超敏C反应蛋白"), ("1", "2"), ("HGB",)):
            with self.subTest(values=values):
                source = model(column("代码", "code_like", values=[
                    {"value": value, "count": 5} for value in values
                ]))
                self.assertEqual(suggestions(source, "map_values")[0], [])

    def test_duplicates_are_first_and_use_all_original_columns(self):
        source = model(column("备注", counts={"normalization_affected": 5}), column("编号", "identifier_like"), duplicates=4)
        issues, recipe = propose_from_profile(source)
        self.assertEqual(recipe["steps"][0]["op"], "flag_duplicates")
        self.assertEqual(recipe["steps"][0]["params"]["key_columns"], ["备注", "编号"])
        self.assertEqual(issues["issues"][0]["affected_rows"], 4)
        self.assertIsNone(issues["issues"][0]["column"])

    def test_output_names_avoid_all_existing_and_previously_generated_columns(self):
        source = model(
            column("年龄", "age_text_like", counts={"age_text": 20}),
            column("年龄_years"), column("年龄_years_2"),
            column("结果", "numeric_with_tokens"),
            column("结果_value"), column("结果_value_2"),
            column("日期", "date_like", counts={"excel_serial_candidate": 5}),
            column("日期_parsed"),
        )
        recipe = propose_from_profile(source)[1]
        self.assertEqual(recipe["steps"][0]["params"]["outputs"]["years"], "年龄_years_3")
        self.assertEqual(recipe["steps"][1]["params"]["outputs"]["value"], "结果_value_3")
        self.assertEqual(recipe["steps"][2]["params"]["output_column"], "日期_parsed_2")

    def test_repeated_calls_and_input_dictionary_order_do_not_change_bytes(self):
        source = model(column("性别", "code_like", values=[{"value": "男", "count": 10}, {"value": "M", "count": 10}]), duplicates=2)
        original = copy.deepcopy(source)
        first = propose_from_profile(source)
        second = propose_from_profile(dict(reversed(list(source.items()))))
        self.assertEqual(json.dumps(first, ensure_ascii=False), json.dumps(second, ensure_ascii=False))
        first[0]["issues"][0]["evidence"][2]["value"].append("修改")
        self.assertEqual(source, original)

    def test_invalid_or_nonpublic_profiles_raise_structured_error(self):
        good = model(column("代码", "code_like", values=[{"value": "男", "count": 20}]))
        cases = [None, {}, {"schema_version": "cleaning-profile/internal/1"}]
        for key, value in (("kind", "unknown"), ("numeric_ratio", float("nan"))):
            altered = copy.deepcopy(good)
            altered["columns"][0][key] = value
            cases.append(altered)
        for value, count in (("罕见合成值", 1), ("13800138000", 5), ("synthetic@example.com", 5)):
            altered = copy.deepcopy(good)
            altered["columns"][0]["values"] = [{"value": value, "count": count}]
            cases.append(altered)
        altered = copy.deepcopy(good)
        altered["columns"][0]["kind"] = "identifier_like"
        cases.append(altered)
        altered = copy.deepcopy(good)
        altered["privacy"]["k"] = 4
        cases.append(altered)
        for case in cases:
            with self.subTest(case=case), self.assertRaises(IssueError) as raised:
                propose_from_profile(case)
            self.assertEqual(raised.exception.code, "profile_invalid")

    def test_extra_fields_cannot_be_smuggled_into_evidence_at_any_object_level(self):
        good = model(
            column("代码", "code_like", values=[{"value": "男", "count": 20}],
                   patterns=[{"pattern": "汉", "count": 20}]),
            column("结果", "numeric_with_tokens",
                   non_numeric_values=[{"value": "阴性", "count": 5}]),
        )
        paths = (
            (), ("input",), ("privacy",), ("row_summary",), ("columns", 0),
            ("columns", 0, "counts"), ("columns", 0, "patterns", 0),
            ("columns", 0, "values", 0), ("columns", 0, "other_values"),
            ("columns", 1, "non_numeric_values", 0),
        )
        for path in paths:
            altered = copy.deepcopy(good)
            target = altered
            for key in path:
                target = target[key]
            target["raw_rows"] = [["只允许留在本机的合成信息"]]
            with self.subTest(path=path), self.assertRaises(IssueError) as raised:
                propose_from_profile(altered)
            self.assertEqual(raised.exception.code, "profile_invalid")

    def test_paths_and_sensitive_formats_are_rejected_in_all_public_text_fields(self):
        unsafe_values = (
            "file=/synthetic", "报告 /synthetic.csv", "Ｃ：＼synthetic＼hidden.csv",
            "13800138000", "synthetic@example.com", "ｓｙｎｔｈｅｔｉｃ＠ｅｘａｍｐｌｅ．ｃｏｍ",
        )
        for text in unsafe_values:
            for field in ("name", "pattern", "value", "non_numeric_value"):
                altered = model(
                    column("代码", "code_like", values=[{"value": "男", "count": 20}],
                           patterns=[{"pattern": "汉", "count": 20}]),
                    column("结果", "numeric_with_tokens",
                           non_numeric_values=[{"value": "阴性", "count": 5}]),
                )
                if field == "name":
                    altered["columns"][0]["name"] = text
                    altered["input"]["columns"][0] = text
                elif field == "pattern":
                    altered["columns"][0]["patterns"][0]["pattern"] = text
                elif field == "value":
                    altered["columns"][0]["values"][0]["value"] = text
                else:
                    altered["columns"][1]["non_numeric_values"][0]["value"] = text
                with self.subTest(text=text, field=field), self.assertRaises(IssueError) as raised:
                    propose_from_profile(altered)
                self.assertEqual(raised.exception.code, "profile_invalid")

    def test_profiles_generated_from_synthetic_fixtures_satisfy_strict_contract(self):
        fixtures = Path(__file__).parent / "fixtures"
        for name in ("lab_example.csv", "profile_workflow.csv"):
            with self.subTest(name=name):
                _, public_profile = profile_csv(fixtures / name)
                issues, recipe = propose_from_profile(public_profile)
                self.assertTrue(issues["issues"])
                validate_recipe(recipe)
