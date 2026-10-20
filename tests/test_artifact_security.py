from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_clean import artifact_security


class ArtifactSecurityTests(unittest.TestCase):
    def test_non_windows_does_not_resolve_paths_or_run_commands(self):
        with patch.object(artifact_security.os, "name", "posix"):
            with patch.object(artifact_security, "Path") as path:
                with patch.object(artifact_security.subprocess, "run") as run:
                    self.assertIsNone(artifact_security.enable_parent_acl_inheritance("synthetic"))
        path.assert_not_called()
        run.assert_not_called()

    @unittest.skipUnless(os.name == "nt", "Windows ACL repair requires Windows")
    def test_windows_repairs_a_temporary_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            child = root / "synthetic"
            child.mkdir()
            (child / "sample.txt").write_text("synthetic", encoding="utf-8")

            self.assertIsNone(artifact_security.enable_parent_acl_inheritance(root))
            self.assertEqual((child / "sample.txt").read_text(encoding="utf-8"), "synthetic")
