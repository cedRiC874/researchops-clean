"""Deterministic recipe execution, atomic publication, and offline replay."""
from __future__ import annotations

import copy
import ctypes
import errno
import hashlib
import json
import os
import platform
import shutil
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from jsonschema import Draft202012Validator

from . import __version__
from .artifact_security import enable_parent_acl_inheritance
from .atomic_io import write_json_atomic
from .csv_io import CSVError, Table, csv_bytes, read_csv
from .ledger import sha256_json
from .ops import OperationError, execute_operation, is_number, operation_catalog
from .paths import OutputDirectoryError, validate_output_directory
from .recipe import RecipeError, load_recipe, recipe_hash, validate_steps


OUTPUT_NAMES = ("cleaned.csv", "changes.csv", "flags.csv")
CHANGE_COLUMNS = ["row", "column", "old_value", "new_value", "step_id", "op"]
FLAG_COLUMNS = ["row", "column", "flag_code", "step_id", "detail"]
_HASH_SCHEMA = {"type": "string", "pattern": "^[0-9a-f]{64}$"}
_STRING_LIST = {"type": "array", "items": {"type": "string"}}


def _object(properties: dict) -> dict:
    return {
        "type": "object",
        "properties": properties,
        "required": list(properties),
        "additionalProperties": False,
    }


_STEP_SCHEMA = _object({
    "step_id": {"type": "string"},
    "op": {"type": "string"},
    "op_version": {"type": "string"},
    "changed_cells": {"type": "integer", "minimum": 0},
    "flagged_cells": {"type": "integer", "minimum": 0},
})
MANIFEST_SCHEMA = _object({
    "schema_version": {"const": "cleaning-run/1"},
    "input": _object({
        "filename": {"type": "string", "minLength": 1, "pattern": r"^[^/\\]+$"},
        "sha256": _HASH_SCHEMA,
        "row_count": {"type": "integer", "minimum": 0},
        "columns": _STRING_LIST,
        "encoding": {"enum": ["utf-8-sig", "utf-8", "gbk", "gb18030"]},
    }),
    "recipe": _object({
        "sha256": _HASH_SCHEMA,
        "step_count": {"type": "integer", "minimum": 0},
    }),
    "outputs": _object({name: _HASH_SCHEMA for name in OUTPUT_NAMES}),
    "steps": {"type": "array", "items": _STEP_SCHEMA},
    "environment": _object({
        "python_version": {"type": "string", "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+[a-z0-9.]*$"},
        "project_version": {"type": "string", "pattern": r"^[0-9]+\.[0-9]+\.[0-9]+$"},
    }),
    "warnings": _object({"formula_like_cells": {"type": "integer", "minimum": 0}}),
    "created_at_utc": {"type": "string", "pattern": r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}Z$"},
    "run_id": {"type": "string", "pattern": "^C-[0-9A-F]{12}$"},
})


class EngineError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class VerificationResult:
    exit_code: int
    code: str
    message: str


def _location(root: Path, value: str | Path) -> Path:
    value = Path(value)
    return value if value.is_absolute() else root / value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _check_input(table: Table, recipe: dict) -> None:
    if table.sha256 != recipe["input"]["sha256"]:
        raise EngineError("input_hash_mismatch", "输入文件的哈希与配方不符。")
    if table.columns != recipe["input"]["columns"]:
        raise EngineError("input_columns_mismatch", "输入表头与配方不一致，列名及顺序必须完全相同。")


def _records_bytes(columns: list[str], records: list[dict]) -> bytes:
    return csv_bytes(columns, [
        {column: str(record[column]) for column in columns} for record in records
    ])


def _execute(table: Table, recipe: dict) -> tuple[dict[str, bytes], list[dict], dict]:
    columns = list(table.columns)
    rows = copy.deepcopy(table.rows)
    changes: list[dict] = []
    flags: list[dict] = []
    summaries: list[dict] = []
    versions = {item["op"]: item["op_version"] for item in operation_catalog()}
    for step in recipe["steps"]:
        step_changes, step_flags = execute_operation(
            step["op"], step["params"], columns, rows, step["step_id"]
        )
        changes.extend(step_changes)
        flags.extend(step_flags)
        summaries.append({
            "step_id": step["step_id"],
            "op": step["op"],
            "op_version": versions[step["op"]],
            "changed_cells": len({(item["row"], item["column"]) for item in step_changes}),
            "flagged_cells": len({(item["row"], item["column"]) for item in step_flags}),
        })
    warnings = {
        "formula_like_cells": sum(
            value.startswith(("=", "+", "-", "@")) and not is_number(value)
            for row in rows for value in row.values()
        )
    }
    outputs = {
        "cleaned.csv": csv_bytes(columns, rows),
        "changes.csv": _records_bytes(CHANGE_COLUMNS, changes),
        "flags.csv": _records_bytes(FLAG_COLUMNS, flags),
    }
    return outputs, summaries, warnings


def _run_id(manifest: dict) -> str:
    # sha256_json uses the existing ledger canonical_json implementation.
    identity = {
        "input_sha256": manifest["input"]["sha256"],
        "recipe_sha256": manifest["recipe"]["sha256"],
        "outputs": manifest["outputs"],
        "steps": manifest["steps"],
        "project_version": manifest["environment"]["project_version"],
    }
    return "C-" + sha256_json(identity)[:12].upper()


def _make_manifest(table: Table, recipe: dict, outputs: dict[str, bytes],
                   summaries: list[dict], warnings: dict) -> dict:
    manifest = {
        "schema_version": "cleaning-run/1",
        "input": {
            "filename": table.filename,
            "sha256": table.sha256,
            "row_count": len(table.rows),
            "columns": table.columns,
            "encoding": table.encoding,
        },
        "recipe": {"sha256": recipe_hash(recipe), "step_count": len(recipe["steps"])},
        "outputs": {name: _sha256(outputs[name]) for name in OUTPUT_NAMES},
        "steps": summaries,
        "environment": {"python_version": platform.python_version(), "project_version": __version__},
        "warnings": warnings,
        "created_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    manifest["run_id"] = _run_id(manifest)
    return manifest


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _rename_without_replacement(source: Path, target: Path) -> None:
    if os.name == "nt":
        # Windows rename already refuses to replace an existing destination.
        source.rename(target)
        return
    # POSIX rename can replace an empty directory, even after an existence
    # check. Linux's no-replace primitive keeps the final publication atomic.
    library = ctypes.CDLL(None, use_errno=True)
    try:
        rename = library.renameat2
    except AttributeError:
        raise EngineError("output_publish_unsupported", "当前系统不支持禁止覆盖的原子目录发布。") from None
    rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    rename.restype = ctypes.c_int
    result = rename(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    if result != 0:
        error = ctypes.get_errno()
        if error == errno.EEXIST:
            raise FileExistsError(error, "Atomic publication destination exists")
        raise OSError(error, "Atomic directory publication failed")


def _publish(target: Path, outputs: dict[str, bytes], manifest: dict) -> None:
    staging: Path | None = None
    missing_parents: list[Path] = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _exists(target):
            raise EngineError("output_exists", "输出目录已存在，不能覆盖。")
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent))
        for name in OUTPUT_NAMES:
            with (staging / name).open("xb") as stream:
                stream.write(outputs[name])
                stream.flush()
                os.fsync(stream.fileno())
        write_json_atomic(staging / "run_manifest.json", manifest)
        if os.name == "nt":
            enable_parent_acl_inheritance(staging)
        if _exists(target):
            raise EngineError("output_exists", "输出目录已存在，不能覆盖。")
        _rename_without_replacement(staging, target)
        staging = None
    except EngineError:
        raise
    except FileExistsError:
        raise EngineError("output_exists", "输出目录已存在，不能覆盖。") from None
    except Exception:
        raise EngineError("output_write_failed", "输出写入或发布失败，未发布半成品。") from None
    finally:
        if staging is not None and staging.exists():
            # Only discard the sibling directory created by this invocation.
            if staging.resolve().parent != target.parent.resolve() or staging.is_symlink():
                raise EngineError("output_cleanup_failed", "临时输出目录校验失败，未执行清理。")
            shutil.rmtree(staging)
        if not _exists(target):
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass


def apply_recipe(input_path: str | Path, recipe_path: str | Path,
                 output_dir: str | Path, *, project_root: str | Path | None = None,
                 encoding: str = "utf-8-sig") -> dict:
    root = Path(project_root) if project_root is not None else Path.cwd()
    root = root.resolve()
    target = validate_output_directory(root, _location(root, output_dir))
    if _exists(target):
        raise EngineError("output_exists", "输出目录已存在，不能覆盖。")
    recipe = load_recipe(_location(root, recipe_path))
    validate_steps(recipe, require_approved=True)
    table = read_csv(_location(root, input_path), encoding=encoding)
    _check_input(table, recipe)
    try:
        outputs, summaries, warnings = _execute(table, recipe)
    except ArithmeticError:
        raise EngineError("numeric_operation_failed", "数值运算超出可处理范围，配方执行失败。") from None
    manifest = _make_manifest(table, recipe, outputs, summaries, warnings)
    _publish(target, outputs, manifest)
    return manifest


def _load_manifest(directory: Path) -> dict:
    path = directory / "run_manifest.json"
    try:
        if path.is_symlink():
            raise EngineError("run_manifest_invalid", "运行记录必须是普通文件。")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if not Draft202012Validator(MANIFEST_SCHEMA).is_valid(manifest):
            raise ValueError("invalid structure")
        datetime.strptime(manifest["created_at_utc"], "%Y-%m-%dT%H:%M:%SZ")
    except FileNotFoundError:
        raise EngineError("run_file_missing", "运行记录或输出文件缺失。") from None
    except (ValueError, OSError, TypeError, RecursionError):
        raise EngineError("run_manifest_invalid", "运行记录无法读取或格式不合法。") from None
    if manifest["run_id"] != _run_id(manifest):
        raise EngineError("run_manifest_mismatch", "运行标识与记录中的哈希和步骤计数不符。")
    return manifest


def verify_run(input_path: str | Path, recipe_path: str | Path, run_dir: str | Path,
               *, project_root: str | Path | None = None) -> VerificationResult:
    """Check recorded hashes first, then independently replay all CSV outputs."""
    root = (Path(project_root) if project_root is not None else Path.cwd()).resolve()
    try:
        directory = validate_output_directory(root, _location(root, run_dir))
        manifest = _load_manifest(directory)
        table = read_csv(_location(root, input_path), encoding=manifest["input"]["encoding"])
        recipe = load_recipe(_location(root, recipe_path))
        if table.sha256 != manifest["input"]["sha256"]:
            raise EngineError("input_hash_mismatch", "输入文件的哈希与运行记录不符。")
        if recipe_hash(recipe) != manifest["recipe"]["sha256"]:
            raise EngineError("recipe_hash_mismatch", "配方的哈希与运行记录不符。")
        _check_input(table, recipe)
        if (table.columns != manifest["input"]["columns"]
                or len(table.rows) != manifest["input"]["row_count"]
                or len(recipe["steps"]) != manifest["recipe"]["step_count"]
                or len(recipe["steps"]) != len(manifest["steps"])):
            raise EngineError("run_manifest_mismatch", "运行记录的输入或步骤信息不一致。")
        recorded = {}
        for name in OUTPUT_NAMES:
            path = directory / name
            if path.is_symlink() or not path.is_file():
                raise EngineError("run_file_missing", "运行记录或输出文件缺失。")
            recorded[name] = path.read_bytes()
            if _sha256(recorded[name]) != manifest["outputs"][name]:
                raise EngineError("output_hash_mismatch", "输出文件的哈希与运行记录不符。")
    except (EngineError, RecipeError, CSVError, OperationError, OutputDirectoryError) as exc:
        return VerificationResult(2, exc.code, exc.message)
    except OSError:
        return VerificationResult(2, "run_file_unreadable", "输入或运行文件无法读取。")
    try:
        validate_steps(recipe, require_approved=True)
        outputs, summaries, warnings = _execute(table, recipe)
        replayed = _make_manifest(table, recipe, outputs, summaries, warnings)
    except (EngineError, RecipeError, CSVError, OperationError, ValueError, ArithmeticError):
        return VerificationResult(1, "replay_failed", "文件哈希完整，但当前执行层无法重放配方。")
    if (any(outputs[name] != recorded[name] for name in OUTPUT_NAMES)
            or replayed["run_id"] != manifest["run_id"]
            or summaries != manifest["steps"] or warnings != manifest["warnings"]):
        return VerificationResult(1, "replay_mismatch", "文件哈希完整，但重新执行的结果不同。")
    return VerificationResult(0, "verified", "全部输出哈希完整，重新执行的三个 CSV 逐字节一致。")
