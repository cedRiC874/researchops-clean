"""Independent benchmark CLI contracts over small synthetic data only."""
from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from researchops_clean.bench.__main__ import main
from researchops_clean.bench.generate import ERROR_TYPES
from researchops_clean.csv_io import read_csv
from researchops_clean.engine import apply_recipe


class BenchmarkCLITests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.artifacts = self.root / "artifacts"
        self.artifacts.mkdir()
        self.bench = self.artifacts / "bench"
        self.run = self.artifacts / "cleaning"
        self.evaluation = self.artifacts / "evaluation"

    def call(self, *arguments):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch("researchops_clean.bench.__main__.Path.cwd", return_value=self.root):
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main([str(argument) for argument in arguments])
        for output in (stdout.getvalue(), stderr.getvalue()):
            self.assertNotIn(str(self.root), output)
            self.assertNotIn(self.root.as_posix(), output)
        return code, stdout.getvalue(), stderr.getvalue()

    def generate(self, *extra):
        return self.call(
            "generate", "--rows", "100", "--seed", "7",
            "--output-dir", self.bench, *extra,
        )

    def uncorrected_run(self):
        code, _, error = self.generate()
        self.assertEqual(code, 0, error)
        table = read_csv(self.bench / "dirty.csv")
        recipe = {
            "schema_version": "cleaning-recipe/1",
            "input": {"sha256": table.sha256, "columns": table.columns},
            "steps": [],
        }
        recipe_path = self.root / "empty-recipe.json"
        recipe_path.write_text(
            json.dumps(recipe, ensure_ascii=False) + "\n", encoding="utf-8", newline="\n"
        )
        apply_recipe(self.bench / "dirty.csv", recipe_path, self.run, project_root=self.root)
        column_map = self.root / "map.json"
        column_map.write_text("{}\n", encoding="utf-8", newline="\n")
        return column_map

    def evaluate(self, column_map, *extra):
        return self.call(
            "evaluate", "--bench-dir", self.bench, "--run-dir", self.run,
            "--column-map", column_map, "--output-dir", self.evaluation, *extra,
        )

    def test_help_exposes_only_the_two_benchmark_commands(self):
        code, stdout, stderr = self.call("--help")
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        self.assertIn("generate", stdout)
        self.assertIn("evaluate", stdout)
        self.assertNotIn("model-propose", stdout)
        for command, expected in (
            ("generate", ("--rows", "--seed", "--output-dir", "--rate")),
            ("evaluate", ("--bench-dir", "--run-dir", "--column-map", "--output-dir")),
        ):
            code, stdout, stderr = self.call(command, "--help")
            self.assertEqual(code, 0)
            self.assertEqual(stderr, "")
            for option in expected:
                self.assertIn(option, stdout)

    def test_default_generation_arguments_reach_generator(self):
        manifest = {
            "parameters": {"rows": 2000, "seed": 7},
            "error_counts": {name: 0 for name in ERROR_TYPES},
            "bench_hash": "a" * 64,
        }
        with patch(
            "researchops_clean.bench.generate.generate_benchmark", return_value=manifest
        ) as generate:
            code, stdout, stderr = self.call("generate", "--output-dir", self.bench)
        self.assertEqual(code, 0)
        self.assertEqual(stderr, "")
        generate.assert_called_once_with(
            2000, 7, output_dir=self.bench, project_root=self.root, rates=None
        )
        self.assertEqual(json.loads(stdout), {
            "status": "success", "command": "generate", "rows": 2000, "seed": 7,
            "error_counts": manifest["error_counts"], "bench_hash": "a" * 64,
        })

    def test_generation_publishes_files_and_reports_actual_cell_counts(self):
        code, stdout, stderr = self.generate(
            "--rate", "sex_code=0.1", "--rate", "duplicate_record=0"
        )
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        manifest = json.loads((self.bench / "bench_manifest.json").read_text(encoding="utf-8"))
        summary = json.loads(stdout)
        self.assertEqual(summary["error_counts"], manifest["error_counts"])
        self.assertEqual(summary["bench_hash"], manifest["bench_hash"])
        self.assertEqual(manifest["parameters"]["rates"]["sex_code"], 0.1)
        self.assertEqual(manifest["parameters"]["rates"]["duplicate_record"], 0)
        self.assertEqual(summary["error_counts"]["duplicate_record"], 0)
        self.assertTrue((self.bench / "README.md").is_file())
        self.assertTrue((self.bench / "reference_column_map.json").is_file())

    def test_malformed_unknown_and_out_of_range_rates_are_rejected(self):
        cases = (
            ("--rate", "sex_code"),
            ("--rate", "=0.1"),
            ("--rate", "sex_code="),
            ("--rate", "sex_code=word"),
            ("--rate", "unknown_type=0.1"),
            ("--rate", "sex_code=-0.1"),
            ("--rate", "sex_code=1.1"),
            ("--rate", "sex_code=nan"),
            ("--rate", "sex_code=inf"),
            ("--rate", "sex_code=0.1", "--rate", "sex_code=0.2"),
        )
        for arguments in cases:
            with self.subTest(arguments=arguments):
                code, stdout, stderr = self.generate(*arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "error")
                self.assertFalse(self.bench.exists())

    def test_bad_arguments_are_structured_and_do_not_echo_paths(self):
        for arguments in (
            ("unknown", self.root / "private.csv"),
            ("generate", "--rows", "not-an-int", "--output-dir", self.bench),
            ("generate", "--rows", "0", "--output-dir", self.bench),
            ("evaluate", "--bench-dir", self.bench),
        ):
            with self.subTest(command=arguments[0]):
                code, stdout, stderr = self.call(*arguments)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "error")
                self.assertNotIn("private.csv", stderr)

    def test_generation_rejects_existing_or_outside_output(self):
        code, _, stderr = self.generate()
        self.assertEqual(code, 0, stderr)
        original = (self.bench / "bench_manifest.json").read_bytes()
        for target in (self.bench, self.artifacts, self.root / "outside"):
            with self.subTest(target=target.name):
                code, stdout, stderr = self.generate("--output-dir", target)
                self.assertEqual(code, 2)
                self.assertEqual(stdout, "")
                self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertEqual((self.bench / "bench_manifest.json").read_bytes(), original)
        self.assertFalse((self.root / "outside").exists())

    def test_successful_evaluation_returns_zero_even_with_missed_errors(self):
        column_map = self.uncorrected_run()
        code, stdout, stderr = self.evaluate(column_map)
        self.assertEqual(code, 0, stderr)
        self.assertEqual(stderr, "")
        result = json.loads(
            (self.evaluation / "evaluation.json").read_text(encoding="utf-8")
        )
        self.assertGreater(sum(item["missed"] for item in result["by_error"].values()), 0)
        self.assertEqual(json.loads(stdout), {
            "status": "success", "command": "evaluate",
            **{key: result[key] for key in (
                "by_error", "false_changes", "false_positives", "bench_hash", "run_hash",
            )},
        })
        self.assertTrue((self.evaluation / "summary.txt").is_file())

    def test_evaluation_rejects_missing_run_files(self):
        column_map = self.uncorrected_run()
        (self.run / "flags.csv").unlink()
        code, stdout, stderr = self.evaluate(column_map)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertFalse(self.evaluation.exists())

    def test_evaluation_rejects_unknown_mapped_column(self):
        column_map = self.uncorrected_run()
        column_map.write_text(
            '{"结果": "不存在的结果列"}\n', encoding="utf-8", newline="\n"
        )
        code, stdout, stderr = self.evaluate(column_map)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertFalse(self.evaluation.exists())

    def test_evaluation_rejects_tampered_run_hashes(self):
        column_map = self.uncorrected_run()
        with (self.run / "cleaned.csv").open("ab") as stream:
            stream.write(b"\n")
        code, stdout, stderr = self.evaluate(column_map)
        self.assertEqual(code, 2)
        self.assertEqual(stdout, "")
        self.assertEqual(json.loads(stderr)["status"], "error")
        self.assertFalse(self.evaluation.exists())


if __name__ == "__main__":
    unittest.main()
