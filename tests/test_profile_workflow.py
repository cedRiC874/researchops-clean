from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.__main__ import main
from researchops_clean import profile_workflow
from researchops_clean.issues import IssueError
from researchops_clean.profile_workflow import ProfileWorkflowError, run_profile
from researchops_clean.recipe import validate_recipe


FIXTURES = Path(__file__).parent / "fixtures"
PROFILE_FILES = {"internal_profile.json", "model_profile.json"}
PROPOSAL_FILES = PROFILE_FILES | {"issues.json", "draft_recipe.json"}


class ProfileWorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.source = FIXTURES / "lab_example.csv"
        self.target = self.artifacts / "profile"

    def run_profile(self, *, propose=False, target=None, **kwargs):
        return run_profile(
            self.source, target or self.target, project_root=self.root,
            propose=propose, **kwargs,
        )

    def test_profile_publishes_only_two_json_documents(self):
        result = self.run_profile()
        self.assertEqual({path.name for path in self.target.iterdir()}, PROFILE_FILES)
        self.assertEqual(result["row_count"], 11)
        self.assertEqual(result["column_count"], 7)
        self.assertEqual(result["issue_count"], 0)
        self.assertEqual(result["category_counts"], {"符合性": 0, "完整性": 0, "合理性": 0})
        for path in self.target.iterdir():
            data = path.read_bytes()
            self.assertFalse(data.startswith(b"\xef\xbb\xbf"))
            self.assertNotIn(b"\r", data)
            text = data.decode("utf-8")
            self.assertNotIn(str(self.root), text)
            self.assertNotIn(self.root.as_posix(), text)
            self.assertNotIn(str(FIXTURES.resolve()), text)
            self.assertIsInstance(json.loads(text), dict)

    def test_propose_is_byte_deterministic_and_all_steps_are_pending(self):
        first = self.run_profile(propose=True)
        other = self.artifacts / "other"
        second = self.run_profile(propose=True, target=other)
        self.assertEqual(first, second)
        self.assertEqual({path.name for path in self.target.iterdir()}, PROPOSAL_FILES)
        for name in PROPOSAL_FILES:
            self.assertEqual((self.target / name).read_bytes(), (other / name).read_bytes())
        draft = json.loads((self.target / "draft_recipe.json").read_text(encoding="utf-8"))
        validate_recipe(draft)
        self.assertTrue(draft["steps"])
        for step in draft["steps"]:
            self.assertEqual(step["approval"], {"status": "pending"})
            self.assertNotIn(step["op"], {"range_check", "convert_unit"})
        issues = json.loads((self.target / "issues.json").read_text(encoding="utf-8"))["issues"]
        self.assertEqual(first["issue_count"], len(issues))
        for category, count in first["category_counts"].items():
            self.assertEqual(count, sum(issue["category"] == category for issue in issues))

    def test_proposal_receives_only_the_public_model_profile(self):
        propose = profile_workflow.propose_from_profile
        received = []

        def inspect(model):
            received.append(json.loads(json.dumps(model, ensure_ascii=False)))
            self.assertEqual(model["schema_version"], "cleaning-profile/model/1")
            self.assertNotIn("0012345", json.dumps(model, ensure_ascii=False))
            return propose(model)

        with patch("researchops_clean.profile_workflow.propose_from_profile", side_effect=inspect):
            self.run_profile(propose=True)
        saved = json.loads((self.target / "model_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(received, [saved])

    def test_existing_output_is_preserved(self):
        self.target.mkdir()
        marker = self.target / "marker.txt"
        marker.write_bytes(b"synthetic")
        with self.assertRaises(ProfileWorkflowError) as caught:
            self.run_profile()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(marker.read_bytes(), b"synthetic")
        self.assertEqual(list(self.target.iterdir()), [marker])

    def test_artifacts_root_and_outside_paths_are_rejected(self):
        for target in (self.artifacts, self.root / "outside", self.artifacts / ".." / "outside"):
            with self.subTest(target=target.name):
                with self.assertRaises(ProfileWorkflowError) as caught:
                    self.run_profile(target=target)
                self.assertEqual(caught.exception.code, "output_path_not_allowed")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_write_failure_cleans_partial_documents_and_new_parents(self):
        write = profile_workflow.write_json_atomic
        calls = []

        def fail_after_first(path, payload):
            calls.append(path.name)
            if len(calls) == 2:
                raise OSError("synthetic")
            write(path, payload)

        with patch("researchops_clean.profile_workflow.write_json_atomic", side_effect=fail_after_first):
            with self.assertRaises(ProfileWorkflowError) as caught:
                self.run_profile(target=self.artifacts / "new-parent" / "run")
        self.assertEqual(caught.exception.code, "output_write_failed")
        self.assertEqual(len(calls), 2)
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_rename_failure_cleans_staging(self):
        with patch("researchops_clean.profile_workflow._rename_without_replacement",
                   side_effect=OSError("synthetic")):
            with self.assertRaises(ProfileWorkflowError) as caught:
                self.run_profile(propose=True)
        self.assertEqual(caught.exception.code, "output_write_failed")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_concurrent_destination_is_not_overwritten(self):
        rename = profile_workflow._rename_without_replacement

        def create_destination(source, target):
            target.mkdir()
            return rename(source, target)

        with patch("researchops_clean.profile_workflow._rename_without_replacement",
                   side_effect=create_destination):
            with self.assertRaises(ProfileWorkflowError) as caught:
                self.run_profile()
        self.assertEqual(caught.exception.code, "output_exists")
        self.assertEqual(list(self.artifacts.iterdir()), [self.target])
        self.assertEqual(list(self.target.iterdir()), [])

    def test_proposal_failure_does_not_publish_even_profiles(self):
        with patch("researchops_clean.profile_workflow.propose_from_profile",
                   side_effect=IssueError("profile_invalid", "合成错误。")):
            with self.assertRaises(ProfileWorkflowError) as caught:
                self.run_profile(propose=True)
        self.assertEqual(caught.exception.code, "profile_invalid")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_invalid_k_never_publishes(self):
        with self.assertRaises(ProfileWorkflowError) as caught:
            self.run_profile(k=4)
        self.assertEqual(caught.exception.code, "profile_invalid_k")
        self.assertEqual(list(self.artifacts.iterdir()), [])

    def test_gbk_profiles_keep_the_hash_of_original_bytes(self):
        source = self.root / "synthetic-gbk.csv"
        data = self.source.read_text(encoding="utf-8").encode("gbk")
        source.write_bytes(data)
        run_profile(source, self.target, project_root=self.root, encoding="gbk")
        import hashlib
        model = json.loads((self.target / "model_profile.json").read_text(encoding="utf-8"))
        self.assertEqual(model["input"]["sha256"], hashlib.sha256(data).hexdigest())


class ProfileEndToEndTests(unittest.TestCase):
    def test_propose_fill_mapping_approve_apply_verify(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "artifacts").mkdir()
            source = (FIXTURES / "profile_workflow.csv").resolve()
            proposed = root / "artifacts" / "propose"
            cleaned = root / "artifacts" / "cleaned"
            filled = root / "filled.json"
            approved = root / "approved.json"

            def cli(*args):
                stdout, stderr = io.StringIO(), io.StringIO()
                with patch("researchops_clean.__main__.Path.cwd", return_value=root):
                    with redirect_stdout(stdout), redirect_stderr(stderr):
                        code = main([str(arg) for arg in args])
                return code, stdout.getvalue(), stderr.getvalue()

            code, _, error = cli("propose", "--input", source, "--output-dir", proposed)
            self.assertEqual(code, 0, error)
            draft = json.loads((proposed / "draft_recipe.json").read_text(encoding="utf-8"))
            self.assertTrue(all(step["approval"]["status"] == "pending" for step in draft["steps"]))
            code, _, error = cli(
                "apply", "--input", source, "--recipe", proposed / "draft_recipe.json",
                "--output-dir", cleaned,
            )
            self.assertEqual(code, 2)
            self.assertIn("recipe_step_not_approved", error)
            self.assertFalse(cleaned.exists())

            mapping_steps = [step for step in draft["steps"] if step["op"] == "map_values"]
            self.assertTrue(mapping_steps)
            explicit_human_mapping = {"M": "男", "男": "男", "F": "女", "女": "女", "A": "A", " Ａ ": "A"}
            for step in mapping_steps:
                self.assertTrue(step["params"]["mapping"])
                for value, target in step["params"]["mapping"].items():
                    self.assertEqual(target, "")
                    self.assertIn(value, explicit_human_mapping)
                    step["params"]["mapping"][value] = explicit_human_mapping[value]
            filled.write_text(json.dumps(draft, ensure_ascii=False, indent=2) + "\n",
                              encoding="utf-8", newline="\n")
            step_ids = ",".join(step["step_id"] for step in draft["steps"])
            code, _, error = cli(
                "approve", "--recipe", filled, "--steps", step_ids,
                "--reviewer", "synthetic-human-reviewer", "--output", approved,
            )
            self.assertEqual(code, 0, error)
            code, _, error = cli("apply", "--input", source, "--recipe", approved, "--output-dir", cleaned)
            self.assertEqual(code, 0, error)
            code, output, error = cli("verify", "--input", source, "--recipe", approved, "--run-dir", cleaned)
            self.assertEqual(code, 0, error)
            self.assertEqual(json.loads(output)["status"], "verified")
            self.assertEqual({path.name for path in cleaned.iterdir()},
                             {"cleaned.csv", "changes.csv", "flags.csv", "run_manifest.json"})
            import csv
            with (cleaned / "cleaned.csv").open(encoding="utf-8", newline="") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 25)
            self.assertEqual({row["性别"] for row in rows}, {"男", "女"})
            self.assertEqual({row["备注"] for row in rows}, {"A"})
            self.assertEqual(sum(row["代码"] == "" for row in rows), 5)
