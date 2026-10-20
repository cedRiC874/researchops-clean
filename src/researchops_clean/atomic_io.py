from __future__ import annotations

import json
import os
import secrets
from pathlib import Path
from typing import Any


def write_json_atomic(path: Path, payload: Any) -> None:
    temporary = path.with_name("." + path.name + "." + secrets.token_hex(4) + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
