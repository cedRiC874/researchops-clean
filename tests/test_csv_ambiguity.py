"""Reject NFC-equivalent headers without changing accepted Unicode text."""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from researchops_clean.csv_io import CSVError, csv_bytes, read_csv


class CSVAmbiguityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name) / "synthetic.csv"

    def test_read_rejects_composed_and_decomposed_equivalent_headers(self):
        self.source.write_bytes("\u00e9,e\u0301\nfirst,second\n".encode("utf-8"))
        with self.assertRaises(CSVError) as caught:
            read_csv(self.source)
        self.assertEqual(caught.exception.code, "ambiguous_column_names")

    def test_read_preserves_a_single_decomposed_header_and_cell(self):
        header = "e\u0301"
        value = " e\u0301\u3000"
        self.source.write_bytes(f"{header}\n{value}\n".encode("utf-8"))
        table = read_csv(self.source)
        self.assertEqual(table.columns, [header])
        self.assertEqual(table.rows, [{header: value}])
        self.assertNotEqual(table.columns[0], "\u00e9")
        self.assertNotEqual(table.rows[0][header], " \u00e9\u3000")

    def test_read_rejects_exact_duplicate_headers_with_the_same_error_code(self):
        self.source.write_bytes(b"result,result\nfirst,second\n")
        with self.assertRaises(CSVError) as caught:
            read_csv(self.source)
        self.assertEqual(caught.exception.code, "ambiguous_column_names")

    def test_read_accepts_distinct_headers_without_compatibility_folding(self):
        self.source.write_bytes("\u00e9,e,A,\uff21\none,two,three,four\n".encode("utf-8"))
        table = read_csv(self.source)
        self.assertEqual(table.columns, ["\u00e9", "e", "A", "\uff21"])
        self.assertEqual(table.rows, [{
            "\u00e9": "one", "e": "two", "A": "three", "\uff21": "four",
        }])

    def test_writer_rejects_nfc_equivalent_and_exact_duplicate_headers(self):
        for columns in (["\u00e9", "e\u0301"], ["result", "result"]):
            with self.subTest(columns=columns):
                with self.assertRaises(CSVError) as caught:
                    csv_bytes(columns, [])
                self.assertEqual(caught.exception.code, "ambiguous_column_names")

    def test_writer_preserves_nonambiguous_unicode_headers_and_cells(self):
        header, value = "e\u0301", " A\u030a "
        data = csv_bytes([header, "other"], [{header: value, "other": "\uff21"}])
        self.assertEqual(data, f"{header},other\n{value},\uff21\n".encode("utf-8"))


if __name__ == "__main__":
    unittest.main()
