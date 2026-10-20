from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_clean.atomic_io import write_json_atomic


class AtomicIOTests(unittest.TestCase):
    def test_successfully_writes_complete_utf8_json(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "synthetic.json"
            payload = {"description": "合成数据", "values": [1, 2, 3]}

            write_json_atomic(target, payload)

            self.assertEqual(json.loads(target.read_text(encoding="utf-8")), payload)
            self.assertNotIn(b"\r", target.read_bytes())
            self.assertEqual(list(root.iterdir()), [target])

    def test_serialization_failure_leaves_no_partial_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "synthetic.json"

            with self.assertRaises(TypeError):
                write_json_atomic(target, {"written_first": "合成数据", "invalid": object()})

            self.assertFalse(target.exists())
            self.assertEqual(list(root.iterdir()), [])

    def test_failed_serialization_preserves_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "synthetic.json"
            original = b'{"synthetic": "previous"}\n'
            target.write_bytes(original)

            with self.assertRaises(ValueError):
                write_json_atomic(target, {"written_first": "synthetic", "invalid": float("nan")})

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [target])

    def test_flush_failure_preserves_existing_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "synthetic.json"
            original = b'{"synthetic": "previous"}\n'
            target.write_bytes(original)

            with patch("researchops_clean.atomic_io.os.fsync", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    write_json_atomic(target, {"synthetic": "new"})

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [target])

    def test_replacement_failure_preserves_existing_file_and_removes_temporary(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target = root / "synthetic.json"
            original = b'{"synthetic": "previous"}\n'
            target.write_bytes(original)

            with patch.object(Path, "replace", side_effect=OSError("synthetic failure")):
                with self.assertRaises(OSError):
                    write_json_atomic(target, {"synthetic": "new"})

            self.assertEqual(target.read_bytes(), original)
            self.assertEqual(list(root.iterdir()), [target])
