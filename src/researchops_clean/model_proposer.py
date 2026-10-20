"""Local model proposals with strict validation, human approval, and an audit trail."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from . import ops
from .artifact_security import enable_parent_acl_inheritance
from .atomic_io import write_json_atomic
from .csv_io import CSVError, read_csv
from .engine import EngineError, _rename_without_replacement
from .issues import IssueError, _INVALID_VALUES, _QUALITATIVE_VALUES, propose_from_profile
from .ledger import AuditError, AuditLedger, canonical_json
from .model_client import (
    PROPOSALS_SCHEMA,
    ModelClientError,
    build_request,
    encode_and_validate_request,
    post_request,
    validate_endpoint,
    validate_options,
)
from .paths import OutputDirectoryError, validate_output_directory
from .profile import ProfileError, SENTINEL_VALUES, build_profiles
from .profile_workflow import _exists, _location
from .recipe import RecipeError, _reject_constant, _unique_object, validate_recipe
from .sensitive_patterns import find_sensitive_types


class ModelProposerError(ValueError):
    def __init__(self, code: str, message: str, exit_code: int = 2):
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code


class ModelOutputError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ModelProposalResult:
    exit_code: int
    record: dict
    proposals: dict


_REJECTION_MESSAGES = {
    "op_not_allowed": "只能使用公开的白名单操作，数值界限必须由医生给出。",
    "params_invalid": "参数不符合该操作的严格校验要求。",
    "column_unknown": "引用的列不在输入列或前面步骤产生的新列中。",
    "output_column_conflict": "新列名重复或与已有列冲突。",
    "value_not_in_profile": "源取值未在对应列的公开取值或固定词表中出现。",
    "reason_invalid": "理由必须为非空且不超过 200 字的文本，并且不能包含敏感格式。",
}
_FIXED_VALUES = frozenset((*SENTINEL_VALUES, *_QUALITATIVE_VALUES, *_INVALID_VALUES))


def _strict_json(text: str | bytes) -> Any:
    value = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
    try:
        # json.loads can turn a valid numeric spelling such as 1e999 into
        # infinity, and can decode lone surrogate escapes. Neither is usable
        # by the existing canonical JSON or UTF-8 publication contract.
        canonical_json(value).encode("utf-8")
    except (AuditError, UnicodeError, RecursionError):
        raise ValueError("Unsupported JSON value") from None
    return value


def _content_proposals(envelope: Any) -> dict:
    try:
        choice = envelope["choices"][0]
        if choice.get("finish_reason") == "length":
            raise ModelOutputError("model_output_truncated", "模型输出被截断，不能生成配方草稿。")
        content = choice["message"]["content"]
        if not isinstance(content, str):
            raise ValueError
        decoded = _strict_json(content)
        if not isinstance(decoded, dict):
            raise ValueError
        return decoded
    except ModelOutputError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, AttributeError, RecursionError):
        raise ModelOutputError("model_output_invalid", "模型回答必须是完整且符合 schema 的 JSON 对象。") from None


def _visible_values(profile: dict) -> dict[str, set[str]]:
    return {
        column["name"]: {
            item["value"]
            for field in ("values", "non_numeric_values")
            for item in column.get(field, [])
        }
        for column in profile["columns"]
    }


def _proposal_identity(op: str, params: dict) -> str:
    # Column names and mapping keys are literal CSV strings. The audit JSON
    # normalizer intentionally folds Unicode to NFC, so it must not determine
    # whether two operations on distinct input columns are duplicates.
    return json.dumps(
        {"op": op, "params": params}, ensure_ascii=False, sort_keys=True,
        separators=(",", ":"), allow_nan=False,
    )


def _value_references(op: str, params: dict) -> list[tuple[str, str]]:
    if op == "map_values":
        return [(params["column"], value) for value in params["mapping"]]
    if op == "sentinel_to_missing":
        return [(params["column"], value) for value in params["values"]]
    if op == "parse_lab_result":
        return [
            (params["column"], value)
            for field in ("qualitative_values", "invalid_values") for value in params[field]
        ]
    if op == "convert_unit":
        references = [
            (params["unit_column"], conversion[field])
            for conversion in params["conversions"] for field in ("from", "to")
        ]
        # A where value is also an observed data value, rather than a new label
        # like a map target or a newly named output column.
        if "where" in params:
            where = params["where"]
            references.extend(
                (where["column"], value)
                for value in ([where["equals"]] if "equals" in where else where["in"])
            )
        return references
    return []


def validate_proposals(
    envelope: dict, model_profile: dict, rule_issues: dict, rule_recipe: dict
) -> tuple[dict, dict]:
    try:
        canonical_json(envelope).encode("utf-8")
        if not isinstance(envelope, dict) or not Draft202012Validator(PROPOSALS_SCHEMA).is_valid(envelope):
            raise ValueError
        proposals = envelope["proposals"]
    except (ValueError, TypeError, RecursionError, AuditError):
        raise ModelOutputError("model_output_invalid", "模型回答必须是完整且符合 schema 的 JSON 对象。") from None
    catalog = {item["op"]: item["params_schema"] for item in ops.operation_catalog()
               if item["op"] != "range_check"}
    visible = _visible_values(model_profile)
    columns = list(rule_recipe["input"]["columns"])
    seen: dict[str, str] = {}
    for step in rule_recipe["steps"]:
        # Rule drafts can deliberately leave parameters for human completion.
        # Their declared output columns are nevertheless available in order.
        for name in ops.declared_output_columns(step["op"], step["params"]):
            if name not in columns:
                columns.append(name)
        seen[_proposal_identity(step["op"], step["params"])] = step["step_id"]
    report: dict[str, list] = {"accepted": [], "rejected": [], "duplicates": []}
    draft = copy.deepcopy(rule_recipe)

    def reject(index: int, op: str, code: str) -> None:
        report["rejected"].append({
            "index": index, "op": op, "error_code": code,
            "message": _REJECTION_MESSAGES[code],
        })

    for index, proposal in enumerate(proposals, 1):
        op, params, reason = proposal["op"], proposal["params"], proposal["reason"]
        if op not in catalog:
            reject(index, op, "op_not_allowed")
            continue
        if not reason.strip() or len(reason) > 200 or find_sensitive_types(reason):
            reject(index, op, "reason_invalid")
            continue
        fingerprint = _proposal_identity(op, params)
        # Exact duplicates must be recognized before new-column collision checks:
        # a rule's parsing outputs have already been added to the column set.
        if fingerprint in seen:
            report["duplicates"].append({
                "index": index, "op": op, "duplicate_of": seen[fingerprint],
            })
            continue
        try:
            new_columns = ops.validate_operation(op, params, columns)
        except ops.OperationError as exc:
            code = "params_invalid"
            if exc.code == "column_missing":
                code = "column_unknown"
            elif Draft202012Validator(catalog[op]).is_valid(params):
                declared = ops.declared_output_columns(op, params)
                normalized_new = [unicodedata.normalize("NFC", name) for name in declared]
                normalized_existing = {unicodedata.normalize("NFC", name) for name in columns}
                if len(normalized_new) != len(set(normalized_new)) or any(
                    name in normalized_existing for name in normalized_new
                ):
                    code = "output_column_conflict"
            reject(index, op, code)
            continue
        warnings = []
        warned = set()
        invalid_value = False
        for column, value in _value_references(op, params):
            if value in visible.get(column, set()):
                continue
            if value not in _FIXED_VALUES:
                invalid_value = True
                break
            if (column, value) not in warned:
                warned.add((column, value))
                warnings.append({
                    "code": "value_unverified", "column": column, "value": value,
                    "message": "该值来自固定词表，但未在对应列公开出现；请在本机核对。",
                })
        if invalid_value:
            reject(index, op, "value_not_in_profile")
            continue
        step = {
            "step_id": f"M{len(report['accepted']) + 1}",
            "op": op,
            "params": copy.deepcopy(params),
            "reason": "【模型建议】" + reason,
            "approval": {"status": "pending"},
        }
        accepted = {"index": index, "step": step, "warnings": warnings}
        if "issue_id" in proposal:
            accepted["issue_id"] = proposal["issue_id"]
        report["accepted"].append(accepted)
        draft["steps"].append(copy.deepcopy(step))
        columns.extend(new_columns)
        seen[fingerprint] = step["step_id"]
    validate_recipe(draft)
    return report, draft


def _response_metadata(envelope: Any) -> tuple[str | None, str | None, dict]:
    if not isinstance(envelope, dict):
        return None, None, {}
    model = envelope.get("model")
    model = model if isinstance(model, str) and not find_sensitive_types(model) else None
    finish_reason = None
    choices = envelope.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        finish_reason = choices[0].get("finish_reason")
        if not isinstance(finish_reason, str) or find_sensitive_types(finish_reason):
            finish_reason = None
    usage = {}
    supplied = envelope.get("usage")
    if isinstance(supplied, dict):
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            value = supplied.get(key)
            if type(value) is int and value >= 0:
                usage[key] = value
        for key, child in (("prompt_tokens_details", "cached_tokens"),
                           ("completion_tokens_details", "reasoning_tokens")):
            nested = supplied.get(key)
            if isinstance(nested, dict) and type(nested.get(child)) is int and nested[child] >= 0:
                usage[key] = {child: nested[child]}
    return model, finish_reason, usage


def _finish_ledger(ledger: AuditLedger, run_id: str) -> dict:
    verification = ledger.verify_chain(run_id).to_dict()
    if not verification["valid"]:
        raise ModelProposerError("model_audit_invalid", "模型提案账本校验失败，不能发布。", 3)
    # AuditLedger closes every connection itself. Explicitly checkpoint and
    # leave DELETE mode so the published directory contains no live sidecars.
    with closing(sqlite3.connect(ledger.database_path)) as connection:
        checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if checkpoint[0] != 0:
            raise ModelProposerError("model_audit_busy", "模型提案账本仍被占用，不能发布。", 3)
        mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0]
        if str(mode).lower() != "delete":
            raise ModelProposerError("model_audit_busy", "模型提案账本无法关闭日志，不能发布。", 3)
    for suffix in ("-wal", "-shm"):
        if ledger.database_path.with_name(ledger.database_path.name + suffix).exists():
            raise ModelProposerError("model_audit_busy", "模型提案账本仍有活动日志，不能发布。", 3)
    return verification


def run_model_proposal(
    input_path: str | Path, endpoint: str, model: str, output_dir: str | Path, *,
    project_root: str | Path | None = None, encoding: str = "utf-8-sig",
    timeout: float = 120, max_tokens: int = 4096, response_format: str = "json_schema",
) -> ModelProposalResult:
    root = (Path(project_root) if project_root is not None else Path.cwd()).resolve()
    try:
        address = validate_endpoint(endpoint)
        validate_options(model, timeout, max_tokens, response_format)
        target = validate_output_directory(root, _location(root, output_dir))
        if _exists(target):
            raise ModelProposerError("output_exists", "输出目录已存在，不能覆盖。")
        source = _location(root, input_path)
        table = read_csv(source, encoding=encoding)
        _, public_profile = build_profiles(table)
        issues, rule_recipe = propose_from_profile(public_profile)
        request = build_request(
            model, public_profile, issues, max_tokens=max_tokens,
            response_format=response_format,
        )
        request_body = encode_and_validate_request(
            request, input_filename=table.filename,
            forbidden_paths=(str(root), root.as_posix(), str(source.resolve()), source.resolve().as_posix()),
        )
    except (CSVError, ProfileError, IssueError, OutputDirectoryError, ModelClientError) as exc:
        raise ModelProposerError(exc.code, exc.message) from None
    except (OSError, RuntimeError):
        raise ModelProposerError("model_input_error", "无法读取或检查提案输入。") from None

    staging = None
    dispatched = False
    missing_parents = []
    parent = target.parent
    while not parent.exists():
        missing_parents.append(parent)
        parent = parent.parent
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        if _exists(target):
            raise ModelProposerError("output_exists", "输出目录已存在，不能覆盖。")
        staging = Path(tempfile.mkdtemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent))
        write_json_atomic(staging / "model_profile.json", public_profile)
        write_json_atomic(staging / "issues.json", issues)
        (staging / "model_request.json").write_bytes(request_body)
        request_hash = hashlib.sha256(request_body).hexdigest()
        ledger = AuditLedger(staging / "ledger.sqlite3", timestamp_format="utc_z")
        run_id = ledger.start_run(
            mode="model_proposal", dataset_sha256=table.sha256,
            request_summary={"request_sha256": request_hash, "rule_step_count": len(rule_recipe["steps"])},
        )
        created_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ledger.append_event(run_id, "proposal_request_attempted", {
            "request_sha256": request_hash, "request_byte_count": len(request_body),
        })
        dispatched = True
        response = post_request(address, request_body, timeout=timeout)
        response_hash = None
        response_name = None
        envelope = None
        if response.body is not None:
            try:
                envelope = _strict_json(response.body.decode("utf-8"))
                response_name = "model_response.json"
            except (ValueError, UnicodeError, RecursionError):
                response_name = "model_response.txt"
            # Preserve received bytes when UTF-8; replace only undecodable bytes
            # so a failed non-text response still leaves a readable local file.
            stored_body = response.body.decode("utf-8", errors="replace").encode("utf-8")
            (staging / response_name).write_bytes(stored_body)
            response_hash = hashlib.sha256(stored_body).hexdigest()
        if response.status is not None:
            ledger.append_event(run_id, "proposal_request_sent", {
                "request_sha256": request_hash, "request_byte_count": len(request_body),
            })
        response_model, finish_reason, usage = _response_metadata(envelope)
        report = {"accepted": [], "rejected": [], "duplicates": []}
        draft = None
        error_code = response.error_code
        error_message = response.message
        if error_code is None:
            try:
                report, draft = validate_proposals(
                    _content_proposals(envelope), public_profile, issues, rule_recipe
                )
            except ModelOutputError as exc:
                error_code, error_message = exc.code, exc.message
        counts = {key: len(report[key]) for key in ("accepted", "rejected", "duplicates")}
        status = "failed" if error_code else "success"
        ledger.record_model_call(
            run_id, provider="openai_compatible_local", model=model,
            started_at_utc=created_at, latency_ms=response.elapsed_ms,
            input_tokens=usage.get("prompt_tokens"), output_tokens=usage.get("completion_tokens"),
            cached_tokens=usage.get("prompt_tokens_details", {}).get("cached_tokens"),
            cost_usd=None, outcome=status, error_code=error_code,
        )
        ledger.append_event(run_id, "proposal_output_validated", {
            "response_sha256": response_hash, "accepted_count": counts["accepted"],
            "rejected_count": counts["rejected"], "duplicate_count": counts["duplicates"],
        })
        ledger.set_run_status(run_id, "failed" if error_code else "completed", terminal_error_code=error_code)
        verification = _finish_ledger(ledger, run_id)
        record = {
            "run_id": run_id, "host": address.host, "port": address.port,
            "requested_model": model, "response_model": response_model,
            "temperature": 0, "seed": 0, "max_tokens": max_tokens,
            "response_format": response_format, "finish_reason": finish_reason,
            "usage": usage, "duration_ms": response.elapsed_ms,
            "counts": counts, "request_sha256": request_hash,
            "response_sha256": response_hash, "response_file": response_name,
            "response_complete": response.complete, "http_status": response.status,
            "status": status, "created_at_utc": created_at,
            "chain_verification": verification,
        }
        if error_code:
            record.update({"error_code": error_code, "message": error_message})
        write_json_atomic(staging / "model_proposals.json", report)
        if draft is not None and not error_code:
            write_json_atomic(staging / "draft_recipe.json", draft)
        write_json_atomic(staging / "run_record.json", record)
        if os.name == "nt":
            enable_parent_acl_inheritance(staging)
        _rename_without_replacement(staging, target)
        staging = None
        return ModelProposalResult(3 if error_code else 0, record, report)
    except ModelProposerError:
        raise
    except FileExistsError:
        raise ModelProposerError("output_exists", "输出目录已存在，不能覆盖。", 3 if dispatched else 2) from None
    except (OSError, sqlite3.Error, RuntimeError, RecipeError, ValueError, TypeError) as exc:
        code = getattr(exc, "code", "model_record_write_failed")
        raise ModelProposerError(
            code, "提案记录写入或发布失败，未覆盖目标目录。", 3 if dispatched else 2
        ) from None
    finally:
        if staging is not None and staging.exists():
            if staging.resolve().parent != target.parent.resolve() or staging.is_symlink():
                raise ModelProposerError("output_cleanup_failed", "临时提案目录校验失败，未执行清理。", 3 if dispatched else 2)
            try:
                shutil.rmtree(staging)
            except OSError:
                raise ModelProposerError(
                    "output_cleanup_failed", "临时提案目录无法清理，请在本机检查。",
                    3 if dispatched else 2,
                ) from None
        if not _exists(target):
            for directory in missing_parents:
                try:
                    directory.rmdir()
                except OSError:
                    pass
