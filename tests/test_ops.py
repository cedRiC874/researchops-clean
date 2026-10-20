from __future__ import annotations

import copy
import unittest
from decimal import Decimal

from jsonschema import Draft202012Validator

from researchops_clean.ops import (
    OperationError,
    decimal_text,
    declared_output_columns,
    execute_operation,
    is_number,
    operation_catalog,
    validate_operation,
)


class OperationTests(unittest.TestCase):
    def execute(self, op, params, values, columns=None):
        columns = list(columns or ["source"])
        rows = [
            dict(value) if isinstance(value, dict) else {"source": value}
            for value in values
        ]
        changes, flags = execute_operation(op, params, columns, rows, "S1")
        return columns, rows, changes, flags

    def assert_invalid(self, op, params, columns=None, code="recipe_invalid"):
        with self.assertRaises(OperationError) as caught:
            validate_operation(op, params, columns or ["source"])
        self.assertEqual(caught.exception.code, code)
        self.assertTrue(caught.exception.message)

    def date_params(self, **overrides):
        params = {
            "column": "source",
            "output_column": "date",
            "formats": ["%Y/%m/%d %H:%M", "%Y%m%d", "%Y-%m-%d"],
            "output": "date",
            "excel_serial": {"enabled": True, "min": 20000, "max": 60000},
        }
        params.update(overrides)
        return params

    def age_params(self):
        return {
            "column": "source",
            "outputs": {"years": "years", "months": "months", "days": "days"},
        }

    def lab_params(self):
        return {
            "column": "source",
            "qualitative_values": ["阴性", "阳性", "+", "++", "+++"],
            "invalid_values": ["溶血", "标本不合格"],
            "outputs": {
                "value": "value",
                "qualifier": "qualifier",
                "qualitative": "qualitative",
                "invalid": "invalid",
            },
        }

    def conversion_params(self, **overrides):
        params = {
            "value_column": "source",
            "unit_column": "unit",
            "conversions": [{"from": "g/dL", "to": "g/L", "factor": "10"}],
        }
        params.update(overrides)
        return params

    def test_catalog_contains_nine_valid_independent_schemas(self):
        catalog = operation_catalog()
        self.assertEqual(
            [item["op"] for item in catalog],
            [
                "normalize_text", "sentinel_to_missing", "map_values",
                "parse_date", "parse_age_cn", "parse_lab_result",
                "convert_unit", "range_check", "flag_duplicates",
            ],
        )
        for item in catalog:
            self.assertEqual(item["op_version"], "1")
            self.assertFalse(item["params_schema"]["additionalProperties"])
            Draft202012Validator.check_schema(item["params_schema"])
        catalog[0]["params_schema"]["properties"].clear()
        self.assertIn("column", operation_catalog()[0]["params_schema"]["properties"])

    def test_number_recognition_accepts_only_ascii_decimal_strings(self):
        for value in ("0", "-0", "+12", "001.20", "-0.50", "123456789012345678901234567890"):
            with self.subTest(value=value):
                self.assertTrue(is_number(value))
        for value in ("", "NaN", "Infinity", "-Infinity", "1e3", "１１０", "١", ".5", "1.", " 1", "1\n"):
            with self.subTest(value=value):
                self.assertFalse(is_number(value))

    def test_decimal_text_preserves_precision_without_exponent_or_trailing_zeros(self):
        cases = {
            "112.0": "112", "0.50": "0.5", "-0.000": "0", "1E+5": "100000",
            "123456789012345678901234567890.00100": "123456789012345678901234567890.001",
        }
        for value, expected in cases.items():
            self.assertEqual(decimal_text(Decimal(value)), expected)
        with self.assertRaises(OperationError):
            decimal_text(Decimal("NaN"))

    def test_normalize_text_is_idempotent_and_records_exact_original_value(self):
        columns, rows, changes, flags = self.execute(
            "normalize_text", {"column": "source"},
            [" \tＡＢＣ　", "　１１０　", "　", "a　b", "", "阴性"],
        )
        self.assertEqual([row["source"] for row in rows], ["ABC", "110", "", "a　b", "", "阴性"])
        self.assertEqual(changes[0], {
            "row": 1, "column": "source", "old_value": " \tＡＢＣ　",
            "new_value": "ABC", "step_id": "S1", "op": "normalize_text",
        })
        self.assertEqual(len(changes), 3)
        self.assertEqual(flags, [])
        original_rows = copy.deepcopy(rows)
        changes, flags = execute_operation("normalize_text", {"column": "source"}, columns, rows, "S2")
        self.assertEqual(rows, original_rows)
        self.assertEqual((changes, flags), ([], []))

    def test_sentinel_matching_is_exact_and_does_not_flag_already_missing(self):
        _, rows, changes, flags = self.execute(
            "sentinel_to_missing", {"column": "source", "values": ["999", "NA", ""]},
            ["999", "999 ", "NA", "", "0"],
        )
        self.assertEqual([row["source"] for row in rows], ["", "999 ", "", "", "0"])
        self.assertEqual([change["old_value"] for change in changes], ["999", "NA"])
        self.assertEqual([flag["flag_code"] for flag in flags], ["sentinel_missing"] * 2)
        self.assertEqual([flag["detail"] for flag in flags], ["999", "NA"])

    def test_map_values_keeps_blank_cells_and_flags_unmapped_values(self):
        _, rows, changes, flags = self.execute(
            "map_values",
            {"column": "source", "mapping": {"M": "男", "男": "男", "": "不可填补"}, "on_unmapped": "keep_and_flag"},
            ["M", "男", "未知", ""],
        )
        self.assertEqual([row["source"] for row in rows], ["男", "男", "未知", ""])
        self.assertEqual(len(changes), 1)
        self.assertEqual([(flag["row"], flag["flag_code"]) for flag in flags], [(3, "unmapped_value")])

    def test_map_values_keep_policy_preserves_unmapped_values(self):
        _, rows, changes, flags = self.execute(
            "map_values",
            {"column": "source", "mapping": {"HGB": "血红蛋白"}, "on_unmapped": "keep"},
            ["CRP", "超敏C反应蛋白", "HGB"],
        )
        self.assertEqual([row["source"] for row in rows], ["CRP", "超敏C反应蛋白", "血红蛋白"])
        self.assertEqual(len(changes), 1)
        self.assertEqual(flags, [])

    def test_map_values_error_policy_rejects_unknown_value(self):
        with self.assertRaises(OperationError) as caught:
            self.execute(
                "map_values",
                {"column": "source", "mapping": {"M": "男"}, "on_unmapped": "error"},
                ["未知"],
            )
        self.assertEqual(caught.exception.code, "unmapped_value")
        self.assert_invalid("map_values", {"column": "source", "mapping": {}, "on_unmapped": "keep"})

    def test_parse_date_supports_formats_excel_and_preserves_source(self):
        values = ["2023/5/6 8:30", "45052", "20230507", "2023-05-08", ""]
        columns, rows, changes, flags = self.execute("parse_date", self.date_params(), values)
        self.assertEqual(columns, ["source", "date"])
        self.assertEqual([row["source"] for row in rows], values)
        self.assertEqual([row["date"] for row in rows], ["2023-05-06", "2023-05-06", "2023-05-07", "2023-05-08", ""])
        self.assertEqual(len(changes), 4)
        self.assertTrue(all(change["old_value"] == "" for change in changes))
        self.assertEqual(flags, [])

    def test_parse_date_datetime_output_uses_minutes(self):
        _, rows, _, _ = self.execute(
            "parse_date", self.date_params(output="datetime"), ["2023/5/6 8:30", "45052"]
        )
        self.assertEqual([row["date"] for row in rows], ["2023-05-06T08:30", "2023-05-06T00:00"])

    def test_parse_date_datetime_output_has_fixed_format_for_offset_input(self):
        _, rows, _, flags = self.execute(
            "parse_date",
            self.date_params(formats=["%Y-%m-%d %H:%M%z"], output="datetime"),
            ["2023-05-06 08:30+0800", "0001-01-02 03:04+0000"],
        )
        self.assertEqual([row["date"] for row in rows], ["2023-05-06T08:30", "0001-01-02T03:04"])
        self.assertEqual(flags, [])

    def test_parse_date_flags_ambiguous_and_unparsed_dates(self):
        _, rows, _, flags = self.execute(
            "parse_date", self.date_params(formats=["%m/%d/%Y", "%d/%m/%Y"]),
            ["05/06/2023", "05/05/2023", "2023年5月", "2023-02-29", ""],
        )
        self.assertEqual([row["date"] for row in rows], ["", "2023-05-05", "", "", ""])
        self.assertEqual(
            [(flag["row"], flag["flag_code"]) for flag in flags],
            [(1, "ambiguous_date"), (3, "unparsed_date"), (4, "unparsed_date")],
        )

    def test_parse_date_excel_limits_are_closed_and_require_integers(self):
        _, rows, _, flags = self.execute(
            "parse_date", self.date_params(excel_serial={"enabled": True, "min": 45052, "max": 45053}),
            ["45051", "45052", "45053", "45054", "45052.0", "４５０５２", "4.5052e4"],
        )
        self.assertEqual([row["date"] for row in rows], ["", "2023-05-06", "2023-05-07", "", "", "", ""])
        self.assertEqual([flag["row"] for flag in flags], [1, 4, 5, 6, 7])
        _, rows, _, flags = self.execute(
            "parse_date", self.date_params(excel_serial={"enabled": False, "min": 20000, "max": 60000}),
            ["45052"],
        )
        self.assertEqual(rows[0]["date"], "")
        self.assertEqual(flags[0]["flag_code"], "unparsed_date")

    def test_parse_age_cn_emits_components_without_conversion(self):
        values = ["3岁2月", "1岁3个月", "3岁", "15天", "5月12天", "2月", "1岁20月40天", "003岁00月"]
        _, rows, _, flags = self.execute("parse_age_cn", self.age_params(), values)
        self.assertEqual(
            [(row["years"], row["months"], row["days"]) for row in rows],
            [("3", "2", "0"), ("1", "3", "0"), ("3", "0", "0"), ("0", "0", "15"),
             ("0", "5", "12"), ("0", "2", "0"), ("1", "20", "40"), ("3", "0", "0")],
        )
        self.assertEqual([row["source"] for row in rows], values)
        self.assertEqual(flags, [])

    def test_parse_age_cn_rejects_invalid_order_decimal_and_unmarked_values(self):
        values = ["", "999", "3.5岁", "2月3岁", "３岁", "3岁junk", "3岁 2月"]
        _, rows, changes, flags = self.execute("parse_age_cn", self.age_params(), values)
        self.assertTrue(all(row["years"] == row["months"] == row["days"] == "" for row in rows))
        self.assertEqual(changes, [])
        self.assertEqual([flag["row"] for flag in flags], list(range(2, 8)))
        self.assertTrue(all(flag["flag_code"] == "unparsed_age" for flag in flags))

    def test_parse_lab_result_supports_numeric_qualifiers_qualitative_and_invalid(self):
        values = ["112.0", "<0.50", ">1000", "≤0.5", "≥5", "<=0.50", ">=5", "阴性", "++", "溶血", ""]
        columns, rows, _, flags = self.execute("parse_lab_result", self.lab_params(), values)
        self.assertEqual(columns, ["source", "value", "qualifier", "qualitative", "invalid"])
        self.assertEqual([row["source"] for row in rows], values)
        self.assertEqual([row["value"] for row in rows], ["112", "0.5", "1000", "0.5", "5", "0.5", "5", "", "", "", ""])
        self.assertEqual([row["qualifier"] for row in rows], ["", "<", ">", "<=", ">=", "<=", ">=", "", "", "", ""])
        self.assertEqual(rows[7]["qualitative"], "阴性")
        self.assertEqual(rows[8]["qualitative"], "++")
        self.assertEqual(rows[9]["invalid"], "1")
        self.assertEqual(flags, [])

    def test_parse_lab_result_does_not_accept_special_or_non_ascii_numbers(self):
        values = ["NaN", "Infinity", "1e2", "１１０", "未知", "< 0.5", "<0.5\n"]
        _, rows, changes, flags = self.execute("parse_lab_result", self.lab_params(), values)
        self.assertEqual(changes, [])
        self.assertTrue(all(row["value"] == row["qualifier"] == row["qualitative"] == row["invalid"] == "" for row in rows))
        self.assertEqual([flag["flag_code"] for flag in flags], ["unparsed_result"] * len(values))

    def test_convert_unit_converts_source_and_preserves_target_units(self):
        columns, rows, changes, flags = self.execute(
            "convert_unit", self.conversion_params(),
            [{"source": "11.2", "unit": "g/dL"}, {"source": "112.0", "unit": "g/L"}],
            ["source", "unit"],
        )
        self.assertEqual(rows, [{"source": "112", "unit": "g/L"}, {"source": "112.0", "unit": "g/L"}])
        self.assertEqual([change["column"] for change in changes], ["source", "unit"])
        self.assertEqual(flags, [])
        changes, flags = execute_operation("convert_unit", self.conversion_params(), columns, rows, "S2")
        self.assertEqual((changes, flags), ([], []))

    def test_convert_unit_multiplies_without_decimal_context_rounding(self):
        _, rows, _, _ = self.execute(
            "convert_unit",
            self.conversion_params(conversions=[{"from": "a", "to": "b", "factor": "1.000000000000000000000000000001"}]),
            [{"source": "123456789012345678901234567890", "unit": "a"}],
            ["source", "unit"],
        )
        self.assertEqual(rows[0]["source"], "123456789012345678901234567890.12345678901234567890123456789")

    def test_convert_unit_flags_missing_unknown_and_invalid_without_changes(self):
        values = [
            {"source": "112", "unit": ""},
            {"source": "112", "unit": "mmol/L"},
            {"source": "NaN", "unit": "g/dL"},
            {"source": "", "unit": "g/dL"},
            {"source": "", "unit": ""},
        ]
        _, rows, changes, flags = self.execute("convert_unit", self.conversion_params(), values, ["source", "unit"])
        self.assertEqual(rows, values)
        self.assertEqual(changes, [])
        self.assertEqual(
            [(flag["row"], flag["flag_code"]) for flag in flags],
            [(1, "missing_unit"), (2, "unknown_unit"), (3, "not_numeric")],
        )

    def test_convert_unit_where_equals_skips_other_analytes(self):
        values = [
            {"source": "11.2", "unit": "g/dL", "item": "血红蛋白"},
            {"source": "5", "unit": "mg/L", "item": "CRP"},
        ]
        _, rows, _, flags = self.execute(
            "convert_unit", self.conversion_params(where={"column": "item", "equals": "血红蛋白"}),
            values, ["source", "unit", "item"],
        )
        self.assertEqual(rows[0]["source"], "112")
        self.assertEqual(rows[1], values[1])
        self.assertEqual(flags, [])

    def test_range_check_is_closed_and_ignores_blank_and_non_numbers(self):
        values = ["29.999", "30", "250", "250.0001", "", "NaN", "１１２", "1e3"]
        _, rows, changes, flags = self.execute(
            "range_check", {"column": "source", "min": "30", "max": "250"}, values
        )
        self.assertEqual([row["source"] for row in rows], values)
        self.assertEqual(changes, [])
        self.assertEqual([(flag["row"], flag["flag_code"]) for flag in flags], [(1, "out_of_range"), (4, "out_of_range")])

    def test_range_check_where_in_selects_only_listed_analytes(self):
        _, _, _, flags = self.execute(
            "range_check",
            {"column": "source", "min": "30", "max": "250", "where": {"column": "item", "in": ["血红蛋白", "Hb"]}},
            [{"source": "1000", "item": "血红蛋白"}, {"source": "1000", "item": "Hb"}, {"source": "1000", "item": "CRP"}],
            ["source", "item"],
        )
        self.assertEqual([flag["row"] for flag in flags], [1, 2])

    def test_duplicate_groups_mark_all_members_in_row_order_without_collisions(self):
        values = [
            {"a": "x,y", "b": "z"}, {"a": "x", "b": "y,z"},
            {"a": "x,y", "b": "z"}, {"a": "", "b": ""},
            {"a": "", "b": ""}, {"a": "x,y", "b": "z"},
        ]
        _, rows, changes, flags = self.execute("flag_duplicates", {"key_columns": ["a", "b"]}, values, ["a", "b"])
        self.assertEqual(rows, values)
        self.assertEqual(changes, [])
        self.assertEqual([flag["row"] for flag in flags], [1, 1, 3, 3, 4, 4, 5, 5, 6, 6])
        self.assertEqual(
            [flag["detail"] for flag in flags],
            ["group_1"] * 4 + ["group_2"] * 4 + ["group_1"] * 2,
        )
        self.assertEqual([flag["column"] for flag in flags], ["a", "b"] * 5)
        self.assertTrue(all(flag["flag_code"] == "duplicate_key" for flag in flags))

    def test_duplicates_use_current_values_after_prior_operations(self):
        columns, rows, _, _ = self.execute("normalize_text", {"column": "source"}, ["Ａ", "A", "B"])
        _, flags = execute_operation("flag_duplicates", {"key_columns": ["source"]}, columns, rows, "S2")
        self.assertEqual([flag["row"] for flag in flags], [1, 2])
        self.assertEqual(flags[0]["detail"], flags[1]["detail"])
        _, flags = execute_operation("flag_duplicates", {"key_columns": ["source"]}, columns, [], "S2")
        self.assertEqual(flags, [])

    def test_validation_rejects_unknown_operation_extra_keys_and_missing_columns(self):
        self.assert_invalid("arbitrary_code", {})
        self.assert_invalid("normalize_text", {"column": "source", "where": {"column": "source", "equals": "x"}})
        self.assert_invalid("normalize_text", {"column": "absent"}, code="column_missing")
        self.assert_invalid("flag_duplicates", {"key_columns": []})
        self.assert_invalid("flag_duplicates", {"key_columns": ["source", "source"]})
        self.assert_invalid("normalize_text", {"column": ""})
        self.assert_invalid("parse_date", self.date_params(output_column=" \t　"))

    def test_validation_checks_new_columns_and_leaves_inputs_unchanged(self):
        columns = ["source"]
        self.assertEqual(validate_operation("parse_age_cn", self.age_params(), columns), ["years", "months", "days"])
        self.assertEqual(columns, ["source"])
        self.assert_invalid("parse_date", self.date_params(output_column="source"))
        params = self.age_params()
        params["outputs"]["days"] = "years"
        self.assert_invalid("parse_age_cn", params)
        params = self.lab_params()
        params["outputs"]["value"] = "source"
        self.assert_invalid("parse_lab_result", params)

    def test_validation_checks_where_shape_and_referenced_columns(self):
        base = {"column": "source", "min": "0", "max": "1"}
        for where in (
            {"column": "source"},
            {"column": "source", "equals": "x", "in": ["x"]},
            {"column": "source", "in": []},
            {"column": "source", "equals": "x", "extra": True},
        ):
            with self.subTest(where=where):
                self.assert_invalid("range_check", {**base, "where": where})
        self.assert_invalid(
            "range_check", {**base, "where": {"column": "absent", "equals": "x"}},
            code="column_missing",
        )

    def test_validation_rejects_invalid_numeric_and_date_boundaries(self):
        for value in ("1e3", "NaN", "Infinity", "１", "1\n", 10):
            with self.subTest(value=value):
                self.assert_invalid("range_check", {"column": "source", "min": value, "max": "10"})
                self.assert_invalid(
                    "convert_unit",
                    self.conversion_params(conversions=[{"from": "a", "to": "b", "factor": value}]),
                    ["source", "unit"],
                )
        self.assert_invalid("range_check", {"column": "source", "min": "20", "max": "10"})
        self.assert_invalid("parse_date", self.date_params(excel_serial={"enabled": True, "min": 2, "max": 1}))

    def test_validation_rejects_conflicting_conversion_and_lab_rules(self):
        for conversions in (
            [{"from": "a", "to": "b", "factor": "1"}, {"from": "a", "to": "c", "factor": "2"}],
            [{"from": "a", "to": "b", "factor": "1"}, {"from": "b", "to": "c", "factor": "2"}],
            [{"from": "a", "to": "a", "factor": "1"}],
        ):
            self.assert_invalid("convert_unit", self.conversion_params(conversions=conversions), ["source", "unit"])
        self.assert_invalid("convert_unit", self.conversion_params(unit_column="source"))
        params = self.lab_params()
        params["invalid_values"].append("阴性")
        self.assert_invalid("parse_lab_result", params)

    def test_declared_output_columns_tolerates_incomplete_drafts(self):
        self.assertEqual(declared_output_columns("parse_date", {"output_column": "date"}), ["date"])
        self.assertEqual(declared_output_columns("parse_age_cn", {"outputs": {"months": "month", "years": "year", "days": 3}}), ["year", "month"])
        self.assertEqual(declared_output_columns("parse_lab_result", {"outputs": {"qualifier": "q", "value": "v"}}), ["v", "q"])
        for op, params in (
            ("unknown", {"output_column": "x"}), ("parse_date", None),
            ("parse_age_cn", {}), ("parse_age_cn", {"outputs": []}),
            ("parse_date", {"output_column": ""}),
            ("parse_date", {"output_column": " \t　"}),
        ):
            self.assertEqual(declared_output_columns(op, params), [])


if __name__ == "__main__":
    unittest.main()
