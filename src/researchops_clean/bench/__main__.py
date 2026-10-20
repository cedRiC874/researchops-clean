"""Independent commands for synthetic benchmark generation and evaluation."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .common import BenchmarkError


class _BenchmarkParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BenchmarkError(
            "benchmark_arguments_invalid", "评测命令参数无效，请用 --help 查看用法。"
        )


def _parser() -> argparse.ArgumentParser:
    parser = _BenchmarkParser(
        prog="python -m researchops_clean.bench",
        description="生成合成检验表并评估清洗结果；参考配方仅用于评测",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    generate = commands.add_parser("generate", help="生成合成标准答案、脏表和错误记录")
    generate.add_argument("--rows", type=int, default=2000)
    generate.add_argument("--seed", type=int, default=7)
    generate.add_argument("--output-dir", type=Path, required=True)
    generate.add_argument(
        "--rate", action="append", default=[], metavar="TYPE=FRACTION",
        help="指定一种错误的注入比例，可为不同类型重复使用",
    )
    evaluate = commands.add_parser("evaluate", help="按标准答案评估已完成的清洗运行")
    evaluate.add_argument("--bench-dir", type=Path, required=True)
    evaluate.add_argument("--run-dir", type=Path, required=True)
    evaluate.add_argument("--column-map", type=Path, required=True)
    evaluate.add_argument("--output-dir", type=Path, required=True)
    return parser


def _rates(arguments: list[str]) -> dict[str, float] | None:
    if not arguments:
        return None
    result = {}
    for item in arguments:
        name, separator, fraction = item.partition("=")
        if not separator or not name or not fraction or name in result:
            raise BenchmarkError(
                "benchmark_arguments_invalid",
                "每个 --rate 须写成 TYPE=FRACTION，且同一错误类型只能指定一次。",
            )
        try:
            result[name] = float(fraction)
        except ValueError:
            raise BenchmarkError(
                "benchmark_arguments_invalid", "错误注入比例须为 0 至 1 之间的数值。"
            ) from None
    return result


def _result(payload: dict, code: int = 0) -> int:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True),
        file=sys.stderr if code else sys.stdout,
    )
    return code


def main(argv: list[str] | None = None) -> int:
    try:
        args = _parser().parse_args(argv)
        if args.command == "generate":
            from .generate import generate_benchmark

            manifest = generate_benchmark(
                args.rows, args.seed, output_dir=args.output_dir,
                project_root=Path.cwd(), rates=_rates(args.rate),
            )
            return _result({
                "status": "success", "command": "generate",
                "rows": manifest["parameters"]["rows"],
                "seed": manifest["parameters"]["seed"],
                "error_counts": manifest["error_counts"],
                "bench_hash": manifest["bench_hash"],
            })
        from .evaluate import evaluate_benchmark

        evaluation = evaluate_benchmark(
            args.bench_dir, args.run_dir, args.column_map, args.output_dir,
            project_root=Path.cwd(),
        )
        return _result({
            "status": "success", "command": "evaluate",
            **{key: evaluation[key] for key in (
                "by_error", "false_changes", "false_positives", "bench_hash", "run_hash",
            )},
        })
    except BenchmarkError as exc:
        return _result(
            {"status": "error", "error_code": exc.code, "message": exc.message}, 2
        )
    except (OSError, UnicodeError):
        return _result({
            "status": "error", "error_code": "benchmark_io_error",
            "message": "无法读写评测所需文件，请检查文件及访问权限。",
        }, 2)
    except SystemExit as exc:
        return int(exc.code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
