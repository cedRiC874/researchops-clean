from __future__ import annotations

from pathlib import Path


class OutputDirectoryError(ValueError):
    """Raised when an output directory is outside the permitted artifact tree."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def validate_output_directory(project_root: Path, output_directory: Path) -> Path:
    artifacts_root = (project_root / "artifacts").resolve()
    resolved = output_directory.resolve()
    if resolved == artifacts_root or not resolved.is_relative_to(artifacts_root):
        raise OutputDirectoryError(
            "output_path_not_allowed",
            "输出目录必须是项目 artifacts/ 下的独立子目录。",
        )
    return resolved
