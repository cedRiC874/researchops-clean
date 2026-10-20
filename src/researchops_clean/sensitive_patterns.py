from __future__ import annotations

import re


_ASCII_PATH_PART = r"[A-Za-z0-9_.-]+"
_RELATIVE_PATH_START = r"(?<![A-Za-z0-9_./\\-])"
_PATTERNS = {
    "identity_number": re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
    "phone_number": re.compile(r"(?<!\d)1\d{10}(?!\d)"),
    "email": re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    "file_path": re.compile(
        r"(?<![A-Za-z0-9_])[A-Za-z]:[\\/]"
        r"|\\\\[^\s\\/]+\\[^\s\\/]+"
        + r"|(?ai:file://)"
        # ASCII segments and an ASCII lookbehind preserve units and Chinese prose.
        # A second slash in an HTTP URL is not the start of a local absolute path.
        + r"|(?<![A-Za-z0-9/])/" + _ASCII_PATH_PART + r"(?:/" + _ASCII_PATH_PART + r")+"
        + r"|" + _RELATIVE_PATH_START + r"(?:~[\\/]|\.{1,2}[\\/])"
        + r"|" + _RELATIVE_PATH_START
        + r"(?:artifacts|data|evals|src|services|output|docs|tests|scripts)[\\/][\w.-]+[\\/]"
        + r"|" + _RELATIVE_PATH_START + r"(?:\.venv|\.git)[\\/]"
        + r"|" + _RELATIVE_PATH_START + r"(?:[\w.-]+[\\/])+[\w.-]+\."
        + r"(?ai:csv|tsv|json|jsonl|parquet|sqlite|sqlite3|db|xlsx|xls|txt|md|py|ps1|yml|yaml|toml|log|zip|data)"
        + r"(?=$|[^\w./\\-]|\.(?=$|\s))"
    ),
    "api_key": re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9._-]+", re.IGNORECASE),
}
