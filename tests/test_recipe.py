from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path

from researchops_clean.ledger import sha256_json
from researchops_clean.recipe import (
    RecipeError,
    approve_recipe,
    load_recipe,
    recipe_hash,
    validate_recipe,
    validate_steps,
)


STAMP = "2026-10-20T08:00:00Z"


def draft_recipe():
    return {
        "schema_version": "cleaning-recipe/1",
        "input": {"sha256": "a" * 64, "columns": ["年龄", "结果"]},
        "steps": [
            {
                "step_id": "S1",
                "op": "normalize_text",
                "params": {"column": "结果"},
                "reason": "手写合成配方",
                "approval": {"status": "pending"},
            }
        ],
    }


class RecipeTests(unittest.TestCase):
    def test_load_and_hash_use_recipe_object_and_existing_canonical_hash(self):
        recipe = draft_recipe()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            path.write_text(json.dumps(recipe, ensure_ascii=False), encoding="utf-8")
            loaded = load_recipe(path)
        self.assertEqual(loaded, recipe)
        self.assertEqual(recipe_hash(loaded), sha256_json(recipe))
        reordered = dict(reversed(list(recipe.items())))
        self.assertEqual(recipe_hash(reordered), recipe_hash(recipe))

    def test_structure_types_version_hash_and_columns_are_checked(self):
        invalid_recipes = [None, [], {}, {"schema_version": "cleaning-recipe/2"}]
        for field, value in (
            ("sha256", "A" * 64),
            ("sha256", "a" * 63),
            ("sha256", "a" * 64 + "\n"),
            ("columns", []),
            ("columns", ["年龄", "年龄"]),
            ("columns", [" "]),
            ("columns", [1]),
        ):
            recipe = draft_recipe()
            recipe["input"][field] = value
            invalid_recipes.append(recipe)
        for recipe in invalid_recipes:
            with self.subTest(recipe=recipe):
                with self.assertRaises(RecipeError) as raised:
                    validate_recipe(recipe)
                self.assertEqual(raised.exception.code, "recipe_invalid")

    def test_step_fields_and_approval_status_are_checked(self):
        for field, value in (
            ("step_id", ""),
            ("op", 1),
            ("params", []),
            ("reason", None),
            ("approval", {"status": "maybe"}),
            ("approval", {"status": "approved"}),
        ):
            with self.subTest(field=field, value=value):
                recipe = draft_recipe()
                recipe["steps"][0][field] = value
                with self.assertRaises(RecipeError):
                    validate_recipe(recipe)

    def test_duplicate_step_ids_are_rejected(self):
        recipe = draft_recipe()
        recipe["steps"].append(copy.deepcopy(recipe["steps"][0]))
        with self.assertRaises(RecipeError) as raised:
            validate_recipe(recipe)
        self.assertEqual(raised.exception.code, "recipe_invalid")

    def test_loading_rejects_duplicate_keys_nonfinite_values_and_bad_json(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "synthetic.json"
            for raw in (
                '{"steps": [], "steps": []}',
                '{"value": NaN}',
                '{"value": Infinity}',
                '{"value": -Infinity}',
                "{",
            ):
                with self.subTest(raw=raw):
                    path.write_text(raw, encoding="utf-8")
                    with self.assertRaises(RecipeError) as raised:
                        load_recipe(path)
                    self.assertEqual(raised.exception.code, "recipe_invalid")

    def test_structure_rejects_nonfinite_numbers_in_unfinished_params(self):
        recipe = draft_recipe()
        recipe["steps"][0]["params"]["draft_number"] = float("nan")
        with self.assertRaises(RecipeError):
            validate_recipe(recipe)

    def test_pending_parameters_can_be_incomplete_but_cannot_be_approved(self):
        recipe = draft_recipe()
        recipe["steps"][0]["params"] = {}
        validate_recipe(recipe)
        with self.assertRaises(RecipeError) as raised:
            approve_recipe(recipe, ["S1"], "reviewer-a", STAMP)
        self.assertEqual(raised.exception.code, "recipe_invalid")

    def test_apply_rejects_all_unapproved_ids_before_parameter_validation(self):
        recipe = draft_recipe()
        recipe["steps"][0]["params"] = {}
        rejected = copy.deepcopy(recipe["steps"][0])
        rejected["step_id"] = "S2"
        rejected["approval"]["status"] = "rejected"
        recipe["steps"].append(rejected)
        with self.assertRaises(RecipeError) as raised:
            validate_steps(recipe)
        self.assertEqual(raised.exception.code, "recipe_step_not_approved")
        self.assertIn("S1", raised.exception.message)
        self.assertIn("S2", raised.exception.message)

    def test_approval_deep_copies_and_preserves_unselected_steps(self):
        recipe = draft_recipe()
        unselected = copy.deepcopy(recipe["steps"][0])
        unselected["step_id"] = "S2"
        unselected["params"] = {}
        recipe["steps"].append(unselected)
        before = copy.deepcopy(recipe)
        approved = approve_recipe(recipe, ["S1"], "reviewer-a", STAMP)
        self.assertEqual(recipe, before)
        self.assertEqual(approved["steps"][1], recipe["steps"][1])
        self.assertEqual(
            approved["steps"][0]["approval"],
            {"status": "approved", "reviewer": "reviewer-a", "decided_at_utc": STAMP},
        )
        self.assertNotEqual(recipe_hash(approved), recipe_hash(recipe))
        approved["steps"][1]["params"]["column"] = "年龄"
        self.assertEqual(recipe["steps"][1]["params"], {})

    def test_default_approval_timestamp_is_utc(self):
        approved = approve_recipe(draft_recipe(), ["S1"], "reviewer-a")
        self.assertTrue(approved["steps"][0]["approval"]["decided_at_utc"].endswith("Z"))
        validate_recipe(approved)

    def test_invalid_reviewers_are_rejected(self):
        for reviewer in ("", "a" * 41, "a/b", "a\\b", "a\nb", "a\x00b", "a\x7fb", "a\x85b"):
            with self.subTest(reviewer=reviewer):
                with self.assertRaises(RecipeError) as raised:
                    approve_recipe(draft_recipe(), ["S1"], reviewer, STAMP)
                self.assertEqual(raised.exception.code, "reviewer_invalid")

    def test_unknown_duplicate_and_empty_approval_selections_are_rejected(self):
        for ids, code in (
            (["missing"], "recipe_step_missing"),
            ([], "recipe_invalid"),
            (["S1", "S1"], "recipe_invalid"),
            ([""], "recipe_invalid"),
        ):
            with self.subTest(ids=ids):
                with self.assertRaises(RecipeError) as raised:
                    approve_recipe(draft_recipe(), ids, "reviewer-a", STAMP)
                self.assertEqual(raised.exception.code, code)

    def test_invalid_or_non_utc_approval_timestamps_are_rejected(self):
        for timestamp in ("yesterday", "2026-02-30T08:00:00Z", "2026-10-20T08:00:00", "2026-10-20T08:00:00+08:00"):
            with self.subTest(timestamp=timestamp):
                with self.assertRaises(RecipeError):
                    approve_recipe(draft_recipe(), ["S1"], "reviewer-a", timestamp)

    def test_unknown_operations_and_missing_columns_are_rejected_when_approved(self):
        for op, params, code in (
            ("unknown", {}, "recipe_invalid"),
            ("normalize_text", {"column": "missing"}, "column_missing"),
            ("normalize_text", {}, "recipe_invalid"),
        ):
            with self.subTest(op=op, params=params):
                recipe = draft_recipe()
                recipe["steps"][0]["op"] = op
                recipe["steps"][0]["params"] = params
                recipe["steps"][0]["approval"] = {
                    "status": "approved", "reviewer": "reviewer-a", "decided_at_utc": STAMP
                }
                validate_recipe(recipe)
                with self.assertRaises(RecipeError) as raised:
                    validate_steps(recipe)
                self.assertEqual(raised.exception.code, code)

    def test_approval_and_validation_allow_references_to_preceding_outputs(self):
        recipe = draft_recipe()
        recipe["steps"][0].update(
            op="parse_age_cn",
            params={"column": "年龄", "outputs": {"years": "岁", "months": "月", "days": "天"}},
        )
        recipe["steps"].append({
            "step_id": "S2",
            "op": "normalize_text",
            "params": {"column": "岁"},
            "reason": "引用前序输出",
            "approval": {"status": "pending"},
        })
        approved = approve_recipe(recipe, ["S1", "S2"], "reviewer-a", STAMP)
        validate_steps(approved)
        reversed_recipe = copy.deepcopy(approved)
        reversed_recipe["steps"].reverse()
        with self.assertRaises(RecipeError) as raised:
            validate_steps(reversed_recipe)
        self.assertEqual(raised.exception.code, "column_missing")

    def test_partial_approval_can_reference_unfinished_preceding_output(self):
        recipe = draft_recipe()
        recipe["steps"][0].update(
            op="parse_age_cn", params={"outputs": {"years": "岁"}}
        )
        recipe["steps"].append({
            "step_id": "S2",
            "op": "normalize_text",
            "params": {"column": "岁"},
            "reason": "待补齐前序规则",
            "approval": {"status": "pending"},
        })
        approved = approve_recipe(recipe, ["S2"], "reviewer-a", STAMP)
        self.assertEqual(approved["steps"][0], recipe["steps"][0])
        self.assertEqual(approved["steps"][1]["approval"]["status"], "approved")
        with self.assertRaises(RecipeError) as raised:
            validate_steps(approved)
        self.assertEqual(raised.exception.code, "recipe_step_not_approved")

    def test_missing_recipe_error_omits_absolute_path(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(RecipeError) as raised:
                load_recipe(Path(directory) / "missing.json")
            self.assertEqual(raised.exception.code, "recipe_read_error")
            self.assertNotIn(directory, raised.exception.message)
