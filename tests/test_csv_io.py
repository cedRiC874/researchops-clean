from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from researchops_clean.csv_io import CSVError, csv_bytes, read_csv, write_csv


CASES = json.loads(
    (Path(__file__).parent / "fixtures" / "csv_cases.json").read_text(encoding="utf-8")
)


class CSVIOTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.input = self.root / "synthetic.csv"

    def test_raw_hash_bom_and_string_values_are_preserved(self):
        raw = b"\xef\xbb\xbf" + CASES["raw_hash_bom"].encode("utf-8")
        self.input.write_bytes(raw)
        table = read_csv(self.input)
        self.assertEqual(table.columns, ["编号", "结果"])
        self.assertEqual(table.rows, [{"编号": "0012345", "结果": "112.0"}, {"编号": "0000000", "结果": ""}])
        self.assertEqual(table.sha256, hashlib.sha256(raw).hexdigest())
        self.assertEqual(table.filename, "synthetic.csv")
        self.assertEqual(table.encoding, "utf-8-sig")

    def test_each_allowed_encoding_reads_chinese(self):
        for encoding in ("utf-8", "utf-8-sig", "gbk", "gb18030"):
            with self.subTest(encoding=encoding):
                self.input.write_bytes(CASES["chinese"].encode(encoding))
                self.assertEqual(read_csv(self.input, encoding).rows[0]["结果"], "阴性")

    def test_unsupported_encoding_is_rejected(self):
        with self.assertRaises(CSVError) as raised:
            read_csv(self.input, "latin-1")
        self.assertEqual(raised.exception.code, "encoding_not_allowed")

    def test_invalid_encoding_bytes_are_rejected(self):
        self.input.write_bytes(bytes.fromhex(CASES["invalid_bytes_hex"]))
        with self.assertRaises(CSVError) as raised:
            read_csv(self.input)
        self.assertEqual(raised.exception.code, "input_decode_error")

    def test_empty_blank_or_duplicate_headers_are_rejected(self):
        for raw in CASES["invalid_headers"]:
            with self.subTest(raw=raw):
                self.input.write_bytes(raw.encode("utf-8"))
                with self.assertRaises(CSVError) as raised:
                    read_csv(self.input)
                self.assertEqual(raised.exception.code, "csv_header_invalid")

    def test_nonrectangular_and_blank_records_are_rejected(self):
        for raw in CASES["nonrectangular"]:
            with self.subTest(raw=raw):
                self.input.write_bytes(raw.encode("utf-8"))
                with self.assertRaises(CSVError) as raised:
                    read_csv(self.input)
                self.assertEqual(raised.exception.code, "csv_row_invalid")

    def test_malformed_quotes_are_rejected(self):
        self.input.write_bytes(CASES["malformed_quotes"].encode("utf-8"))
        with self.assertRaises(CSVError) as raised:
            read_csv(self.input)
        self.assertEqual(raised.exception.code, "csv_invalid")

    def test_size_limit_includes_original_bytes_and_accepts_boundary(self):
        self.input.write_bytes(CASES["size_boundary"].encode("utf-8"))
        with patch("researchops_clean.csv_io.MAX_INPUT_BYTES", 4):
            self.assertEqual(len(read_csv(self.input).rows), 1)
        with patch("researchops_clean.csv_io.MAX_INPUT_BYTES", 3):
            with self.assertRaises(CSVError) as raised:
                read_csv(self.input)
        self.assertEqual(raised.exception.code, "input_too_large")

    def test_row_limit_counts_records_and_accepts_boundary(self):
        self.input.write_bytes(CASES["row_boundary"].encode("utf-8"))
        with patch("researchops_clean.csv_io.MAX_DATA_ROWS", 2):
            self.assertEqual(len(read_csv(self.input).rows), 2)
        with patch("researchops_clean.csv_io.MAX_DATA_ROWS", 1):
            with self.assertRaises(CSVError) as raised:
                read_csv(self.input)
        self.assertEqual(raised.exception.code, "input_too_many_rows")

    def test_long_field_within_file_limit_and_csv_global_limit_restored(self):
        original = csv.field_size_limit()
        self.input.write_bytes(
            (CASES["long_field_header"] + CASES["long_field_seed"] * 140_000 + "\n").encode("utf-8")
        )
        self.assertEqual(len(read_csv(self.input).rows[0]["a"]), 140_000)
        self.assertEqual(csv.field_size_limit(), original)

    def test_writer_reproducible_utf8_lf_and_quoted_cells_round_trip(self):
        columns = CASES["writer_columns"]
        rows = CASES["writer_rows"]
        expected = CASES["writer_expected"].encode("utf-8")
        payload = csv_bytes(columns, rows)
        self.assertEqual(payload, expected)
        write_csv(self.input, columns, rows)
        self.assertEqual(self.input.read_bytes(), payload)
        self.assertEqual(read_csv(self.input).rows, rows)

    def test_writer_allows_header_without_data_rows(self):
        self.assertEqual(csv_bytes(["a"], []), b"a\n")

    def test_embedded_carriage_returns_are_quoted_and_preserved(self):
        columns = CASES["carriage_return_columns"]
        rows = CASES["carriage_return_rows"]
        write_csv(self.input, columns, rows)
        self.assertEqual(
            self.input.read_bytes(), CASES["carriage_return_expected"].encode("utf-8")
        )
        self.assertEqual(read_csv(self.input).rows, rows)

    def test_writer_rejects_missing_extra_or_nonstring_values(self):
        for row in ({}, {"a": "1", "extra": "2"}, {"a": None}, {"a": 1}):
            with self.subTest(row=row):
                with self.assertRaises(CSVError):
                    csv_bytes(["a"], [row])

    def test_io_errors_do_not_reveal_absolute_paths(self):
        with self.assertRaises(CSVError) as raised:
            read_csv(self.input)
        self.assertEqual(raised.exception.code, "input_read_error")
        self.assertNotIn(str(self.root), str(raised.exception))
        with self.assertRaises(CSVError) as raised:
            write_csv(self.root / "missing" / "output.csv", ["a"], [])
        self.assertEqual(raised.exception.code, "output_write_error")
        self.assertNotIn(str(self.root), str(raised.exception))
