from __future__ import annotations

from pathlib import Path


class OutputDirectoryError(ValueError):
    """Raised when an output directory is outside the permitted artifact tree."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_output_directory(project_root: Path, session_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = session_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise OutputDirectoryError(
            "self_pilot_path_not_allowed",
            "Self-pilot session 必须位于项目 artifacts 的独立子目录。",
        )
    return resolved
