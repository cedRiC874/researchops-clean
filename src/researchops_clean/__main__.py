"""Human-operated command line for offline recipe approval and execution."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .csv_io import CSVError
from .engine import EngineError, apply_recipe, verify_run
from .ops import OperationError, operation_catalog
from .paths import OutputDirectoryError
from .recipe import RecipeError, approve_recipe, load_recipe, recipe_hash


class CLIError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse's original message can echo user-supplied absolute paths.
        raise CLIError("cli_invalid_arguments", "命令行参数无效，请用 --help 查看用法。")


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="python -m researchops_clean", description="离线配方清洗与重放核验")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("ops", help="输出操作名称、版本和参数 schema")
    approve = commands.add_parser("approve", help="人工批准指定步骤并另存配方")
    approve.add_argument("--recipe", required=True, type=Path)
    approve.add_argument("--steps", required=True, help="逗号分隔的 step_id，如 S1,S2")
    approve.add_argument("--reviewer", required=True)
    approve.add_argument("--output", required=True, type=Path)
    apply = commands.add_parser("apply", help="执行已批准配方")
    apply.add_argument("--input", required=True, type=Path)
    apply.add_argument("--recipe", required=True, type=Path)
    apply.add_argument("--output-dir", required=True, type=Path)
    apply.add_argument("--encoding", default="utf-8-sig",
                       choices=("utf-8-sig", "utf-8", "gbk", "gb18030"))
    verify = commands.add_parser("verify", help="检查文件完整性并重放配方")
    verify.add_argument("--input", required=True, type=Path)
    verify.add_argument("--recipe", required=True, type=Path)
    verify.add_argument("--run-dir", required=True, type=Path)
    for name, help_text in (
        ("profile", "生成本机剖析与模型可见剖析"),
        ("propose", "生成剖析、问题清单和待批准配方草稿"),
    ):
        profile_command = commands.add_parser(name, help=help_text)
        profile_command.add_argument("--input", required=True, type=Path)
        profile_command.add_argument("--output-dir", required=True, type=Path)
        profile_command.add_argument("--k", default=5, type=int)
        profile_command.add_argument("--encoding", default="utf-8-sig",
                                     choices=("utf-8-sig", "utf-8", "gbk", "gb18030"))
    return parser


def _emit(value: object, *, error: bool = False) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True),
          file=sys.stderr if error else sys.stdout)


def _save_approved(path: Path, recipe: dict) -> None:
    payload = json.dumps(recipe, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    created = False
    try:
        with path.open("x", encoding="utf-8", newline="\n") as stream:
            created = True
            stream.write(payload)
    except FileExistsError:
        raise CLIError("output_exists", "目标配方文件已存在，不能覆盖。") from None
    except (OSError, UnicodeError):
        if created:
            try:
                path.unlink()
            except OSError:
                raise CLIError("output_cleanup_failed", "配方写入失败，请检查未完成的目标文件。") from None
        raise CLIError("output_write_failed", "无法写入新配方文件。") from None


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command in ("profile", "propose"):
            from .profile_workflow import ProfileWorkflowError, run_profile

            if args.k < 5:
                raise CLIError("profile_invalid_k", "k 必须是大于或等于 5 的整数。")
            try:
                summary = run_profile(args.input, args.output_dir, project_root=Path.cwd(),
                                      k=args.k, encoding=args.encoding,
                                      propose=args.command == "propose")
            except ProfileWorkflowError as exc:
                raise CLIError(exc.code, exc.message) from None
            _emit({"status": "success", **summary})
            return 0
        if args.command == "ops":
            _emit(operation_catalog())
            return 0
        if args.command == "approve":
            step_ids = [value.strip() for value in args.steps.split(",")]
            if any(not step_id for step_id in step_ids):
                raise CLIError("recipe_invalid", "必须指定非空的步骤编号。")
            approved = approve_recipe(load_recipe(args.recipe), step_ids, args.reviewer)
            digest = recipe_hash(approved)
            _save_approved(args.output, approved)
            _emit({"status": "approved", "recipe_sha256": digest,
                   "message": "指定步骤已批准并写入新配方。"})
            return 0
        if args.command == "apply":
            manifest = apply_recipe(args.input, args.recipe, args.output_dir,
                                    project_root=Path.cwd(), encoding=args.encoding)
            _emit({"status": "success", "run_id": manifest["run_id"],
                   "message": "配方执行完成，四个输出文件已发布。"})
            return 0
        result = verify_run(args.input, args.recipe, args.run_dir, project_root=Path.cwd())
        _emit({"status": {0: "verified", 1: "different", 2: "invalid"}[result.exit_code],
               "code": result.code, "message": result.message})
        return result.exit_code
    except (CSVError, RecipeError, OperationError, EngineError, OutputDirectoryError, CLIError) as exc:
        _emit({"status": "error", "error_code": exc.code, "message": exc.message}, error=True)
        return 2
    except (OSError, UnicodeError):
        _emit({"status": "error", "error_code": "file_io_error",
               "message": "无法读写所需文件，请检查文件及访问权限。"}, error=True)
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
