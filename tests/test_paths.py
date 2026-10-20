from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from researchops_clean.paths import OutputDirectoryError, validate_output_directory


class OutputDirectoryTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)

    def test_accepts_an_independent_artifact_subdirectory(self):
        target = self.root / "artifacts" / "synthetic-run" / "output"
        self.assertEqual(validate_output_directory(self.root, target), target.resolve())

    def test_rejects_the_artifacts_directory_itself(self):
        with self.assertRaises(OutputDirectoryError):
            validate_output_directory(self.root, self.root / "artifacts")

    def test_rejects_a_directory_outside_artifacts(self):
        with self.assertRaises(OutputDirectoryError):
            validate_output_directory(self.root, self.root / "outside")

    def test_rejects_parent_traversal_escaping_artifacts(self):
        target = self.root / "artifacts" / "synthetic-run" / ".." / ".." / "outside"
        with self.assertRaises(OutputDirectoryError):
            validate_output_directory(self.root, target)

    def test_rejects_parent_traversal_resolving_to_artifacts(self):
        target = self.root / "artifacts" / "synthetic-run" / ".."
        with self.assertRaises(OutputDirectoryError):
            validate_output_directory(self.root, target)
