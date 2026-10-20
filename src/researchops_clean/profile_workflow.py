"""Local, deterministic publication of profiles and pending recipe proposals."""
from __future__ import annotations

import copy
import os
import shutil
import tempfile
from pathlib import Path

from .artifact_security import enable_parent_acl_inheritance
from .atomic_io import write_json_atomic
from .csv_io import CSVError
from .engine import EngineError, _rename_without_replacement
from .issues import IssueError, propose_from_profile
from .paths import OutputDirectoryError, validate_output_directory
from .profile import ProfileError, profile_csv


class ProfileWorkflowError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _location(root: Path, value: str | Path) -> Path:
    value = Path(value)
    return value if value.is_absolute() else root / value


def _exists(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _publish_json(target: Path, documents: dict[str, dict]) -> None:
    staging: Path | None = None
    missing_parents = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _exists(target):
            raise ProfileWorkflowError("output_exists", "输出目录已存在，不能覆盖。")
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent))
        for filename, document in documents.items():
            write_json_atomic(staging / filename, document)
        if os.name == "nt":
            enable_parent_acl_inheritance(staging)
        _rename_without_replacement(staging, target)
        staging = None
    except ProfileWorkflowError:
        raise
    except FileExistsError:
        raise ProfileWorkflowError("output_exists", "输出目录已存在，不能覆盖。") from None
    except EngineError as exc:
        raise ProfileWorkflowError(exc.code, exc.message) from None
    except Exception:
        raise ProfileWorkflowError("output_write_failed", "剖析结果写入或发布失败，未发布半成品。") from None
    finally:
        if staging is not None and staging.exists():
            if staging.resolve().parent != target.parent.resolve() or staging.is_symlink():
                raise ProfileWorkflowError("output_cleanup_failed", "临时输出目录校验失败，未执行清理。")
            shutil.rmtree(staging)
        if not _exists(target):
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass


def run_profile(input_path: str | Path, output_dir: str | Path, *,
                project_root: str | Path | None = None, k: int = 5,
                encoding: str = "utf-8-sig", propose: bool = False) -> dict:
    root = (Path(project_root) if project_root is not None else Path.cwd()).resolve()
    try:
        target = validate_output_directory(root, _location(root, output_dir))
        if _exists(target):
            raise ProfileWorkflowError("output_exists", "输出目录已存在，不能覆盖。")
        internal, model = profile_csv(_location(root, input_path), k=k, encoding=encoding)
        documents = {"internal_profile.json": internal, "model_profile.json": model}
        cards = []
        if propose:
            # Proposals receive only the same filtered object intended for the
            # future model. No table or internal-profile values cross here.
            issues, draft = propose_from_profile(copy.deepcopy(model))
            cards = issues["issues"]
            documents.update({"issues.json": issues, "draft_recipe.json": draft})
        _publish_json(target, documents)
    except (CSVError, ProfileError, IssueError, EngineError, OutputDirectoryError) as exc:
        raise ProfileWorkflowError(exc.code, exc.message) from None
    except OSError:
        raise ProfileWorkflowError("profile_io_error", "无法读写剖析所需文件。") from None
    return {
        "row_count": model["input"]["row_count"],
        "column_count": len(model["input"]["columns"]),
        "issue_count": len(cards),
        "category_counts": {
            category: sum(card["category"] == category for card in cards)
            for category in ("符合性", "完整性", "合理性")
        },
    }
