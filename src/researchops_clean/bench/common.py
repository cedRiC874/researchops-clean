"""Shared serialization and publication for synthetic benchmark artifacts."""
from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from ..artifact_security import ArtifactPermissionError, enable_parent_acl_inheritance
from ..engine import EngineError, _rename_without_replacement
from ..ledger import canonical_json
from ..paths import OutputDirectoryError, validate_output_directory
from ..profile_workflow import _exists, _location


GENERATOR_VERSION = "1"
FIXED_TIMESTAMP = "2026-01-01T00:00:00Z"


class BenchmarkError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def json_bytes(value: Any) -> bytes:
    return (canonical_json(value) + "\n").encode("utf-8")


def project_path(value: str | Path, project_root: str | Path | None = None) -> Path:
    root = (Path(project_root) if project_root is not None else Path.cwd()).resolve()
    return _location(root, value)


def validate_target(output_dir: str | Path, project_root: str | Path | None = None) -> Path:
    root = (Path(project_root) if project_root is not None else Path.cwd()).resolve()
    try:
        target = validate_output_directory(root, _location(root, output_dir))
    except (OutputDirectoryError, OSError, RuntimeError):
        raise BenchmarkError("output_path_not_allowed", "输出目录必须是项目 artifacts/ 下的独立子目录。") from None
    if _exists(target):
        raise BenchmarkError("output_exists", "输出目录已存在，不能覆盖。")
    return target


def publish_files(output_dir: str | Path, files: dict[str, bytes], *,
                  project_root: str | Path | None = None) -> Path:
    target = validate_target(output_dir, project_root)
    if any(
        not isinstance(name, str) or name in {"", ".", ".."}
        or "/" in name or "\\" in name or ":" in name or not isinstance(data, bytes)
        for name, data in files.items()
    ):
        raise BenchmarkError("output_invalid", "评测产物必须使用独立文件名和字节内容。")
    missing_parents = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent) as temporary:
            staging = Path(temporary)
            if staging.resolve().parent != target.parent.resolve() or staging.is_symlink():
                raise BenchmarkError("output_invalid", "临时输出目录校验失败。")
            for name, data in files.items():
                with (staging / name).open("xb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            if os.name == "nt":
                enable_parent_acl_inheritance(staging)
            _rename_without_replacement(staging, target)
    except FileExistsError:
        raise BenchmarkError("output_exists", "输出目录已存在，不能覆盖。") from None
    except EngineError as exc:
        raise BenchmarkError(exc.code, exc.message) from None
    except (OSError, ArtifactPermissionError):
        raise BenchmarkError("output_write_failed", "评测产物写入或发布失败。") from None
    finally:
        if not _exists(target):
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass
    return target
