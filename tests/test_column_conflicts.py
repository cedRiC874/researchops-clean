"""NFC-aware output conflicts keep existing column references exact."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean.csv_io import CSVError, csv_bytes, read_csv
from researchops_clean.issues import propose_from_profile
from researchops_clean.model_proposer import validate_proposals
from researchops_clean.ops import OperationError, validate_operation
from researchops_clean.profile import profile_csv


class ColumnConflictTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.source = self.root / "synthetic.csv"
        self.composed = "caf\u00e9"
        self.decomposed = "cafe\u0301"
        self.columns = [self.composed, "数值", "单位"]
        rows = [
            {self.composed: "2024-01-01", "数值": "1.0", "单位": unit}
            for unit in ("mg", "g") for _ in range(5)
        ]
        self.source.write_bytes(csv_bytes(self.columns, rows))
        _, self.profile = profile_csv(self.source)
        self.issues, self.rule_recipe = propose_from_profile(self.profile)

    def date_params(self, output_column):
        return {
            "column": self.composed, "output_column": output_column,
            "formats": ["%Y-%m-%d"], "output": "date",
            "excel_serial": {"enabled": False, "min": 20000, "max": 60000},
        }

    def model_report(self, op, params):
        return validate_proposals(
            {"proposals": [{"op": op, "params": params, "reason": "合成列名校验"}]},
            self.profile, self.issues, self.rule_recipe,
        )

    def test_approve_cli_rejects_output_nfc_equivalent_to_existing_column(self):
        recipe = {
            "schema_version": "cleaning-recipe/1",
            "input": {
                "sha256": read_csv(self.source).sha256,
                "columns": self.columns,
            },
            "steps": [{
                "step_id": "S1", "op": "parse_date",
                "params": self.date_params(self.decomposed),
                "reason": "合成输出列冲突",
                "approval": {"status": "pending"},
            }],
        }
        recipe_path, approved_path = self.root / "draft.json", self.root / "approved.json"
        recipe_path.write_text(
            json.dumps(recipe, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n",
        )
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([
                    "approve", "--recipe", str(recipe_path), "--steps", "S1",
                    "--reviewer", "synthetic-reviewer", "--output", str(approved_path),
                ])
        self.assertEqual(code, 2)
        self.assertEqual(stdout.getvalue(), "")
        self.assertEqual(json.loads(stderr.getvalue())["error_code"], "recipe_invalid")
        self.assertFalse(approved_path.exists())

    def test_model_rejects_output_nfc_equivalent_to_existing_column(self):
        report, draft = self.model_report("parse_date", self.date_params(self.decomposed))
        self.assertEqual(report["accepted"], [])
        self.assertEqual(report["duplicates"], [])
        self.assertEqual(report["rejected"][0]["error_code"], "output_column_conflict")
        self.assertEqual(draft["steps"], self.rule_recipe["steps"])

    def test_derived_columns_cannot_be_nfc_equivalent_to_each_other(self):
        params = {
            "column": self.composed,
            "outputs": {"years": "新\u00e9", "months": "新e\u0301", "days": "新天"},
        }
        with self.assertRaises(OperationError) as caught:
            validate_operation("parse_age_cn", params, self.columns)
        self.assertEqual(caught.exception.code, "recipe_invalid")
        report, _ = self.model_report("parse_age_cn", params)
        self.assertEqual(report["accepted"], [])
        self.assertEqual(report["rejected"][0]["error_code"], "output_column_conflict")

    def test_source_key_and_where_references_are_not_normalized(self):
        cases = [
            ("normalize_text", {"column": self.decomposed}),
            ("flag_duplicates", {"key_columns": [self.decomposed]}),
            ("convert_unit", {
                "value_column": "数值", "unit_column": "单位",
                "conversions": [{"from": "mg", "to": "g", "factor": "0.001"}],
                "where": {"column": self.decomposed, "equals": "2024-01-01"},
            }),
        ]
        for op, params in cases:
            with self.subTest(op=op):
                with self.assertRaises(OperationError) as caught:
                    validate_operation(op, params, self.columns)
                self.assertEqual(caught.exception.code, "column_missing")
                report, _ = self.model_report(op, params)
                self.assertEqual(report["accepted"], [])
                self.assertEqual(report["rejected"][0]["error_code"], "column_unknown")

    def test_distinct_new_columns_are_accepted_and_preserve_original_spelling(self):
        output = "新e\u0301"
        params = self.date_params(output)
        self.assertEqual(validate_operation("parse_date", params, self.columns), [output])
        report, draft = self.model_report("parse_date", params)
        self.assertEqual(report["rejected"], [])
        self.assertEqual(len(report["accepted"]), 1)
        self.assertEqual(report["accepted"][0]["step"]["params"]["output_column"], output)
        self.assertEqual(draft["steps"][-1]["params"]["output_column"], output)
        self.assertNotEqual(output, "新\u00e9")

    def test_csv_duplicate_diagnostics_show_positions_without_column_names(self):
        path = self.root / "duplicate.csv"
        for repeated in (self.composed, self.decomposed):
            with self.subTest(repeated=repeated):
                text = f"甲,{self.composed},乙,丙,{repeated}\n1,2,3,4,5\n"
                path.write_bytes(text.encode("utf-8"))
                with self.assertRaises(CSVError) as caught:
                    read_csv(path)
                self.assertEqual(caught.exception.code, "ambiguous_column_names")
                message = caught.exception.message
                self.assertIn("2", message)
                self.assertIn("5", message)
                self.assertNotIn(self.composed, message)
                self.assertNotIn(self.decomposed, message)
                self.assertIn("编码", message)
                self.assertRegex(message, "相同|一样")


if __name__ == "__main__":
    unittest.main()
