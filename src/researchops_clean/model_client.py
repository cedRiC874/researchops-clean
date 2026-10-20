"""A single, bounded request to a local OpenAI-compatible model endpoint."""

from __future__ import annotations

import http.client
import json
import math
import re
import socket
import time
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from .ops import operation_catalog
from .recipe import _reject_constant, _unique_object
from .sensitive_patterns import find_sensitive_types


MAX_REQUEST_BYTES = 256 * 1024
MAX_RESPONSE_BYTES = 1024 * 1024

PROPOSALS_SCHEMA = {
    "type": "object",
    "properties": {
        "proposals": {
            "type": "array",
            "maxItems": 30,
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string"},
                    "params": {"type": "object"},
                    "reason": {"type": "string"},
                    "issue_id": {"type": "string"},
                },
                "required": ["op", "params", "reason"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["proposals"],
    "additionalProperties": False,
}

SYSTEM_PROMPT = (
    "你是数据清洗方案助手，只提出待人工批准的建议，不执行任何操作。"
    "只能使用 operations 中列出的操作，参数必须符合各自的 schema。"
    "引用的列必须存在于输入列或前面步骤产生的列中。"
    "取值只能来自 model_profile 中可见的取值或程序已有的固定词表；不要猜测隐藏取值。"
    "不要提出数值界限：range_check 必须由医生提供界限。"
    "不要提出删除行或填补缺失的方案。"
    "每条 reason 必须非空且不超过 200 字，不包含敏感信息。"
    "只输出符合给定 schema 的一个完整 JSON 对象，不加解释、思考文字或代码围栏，"
    "其中 proposals 为数组，最多 30 条，每条仅含必需的 op、params、reason 和可选的 issue_id。"
)


class ModelClientError(ValueError):
    """A request was rejected before any connection was attempted."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Endpoint:
    url: str
    host: str
    port: int
    scheme: str


@dataclass(frozen=True)
class TransportResult:
    status: int | None
    body: bytes | None
    elapsed_ms: float
    error_code: str | None = None
    message: str = ""
    complete: bool = True


def validate_endpoint(endpoint: str) -> Endpoint:
    """Accept only literal loopback hosts, without URL credentials or extras."""
    invalid = ModelClientError("endpoint_not_allowed", "模型地址必须是无凭据、查询串和片段的本机 HTTP 或 HTTPS 地址。")
    if (
        not isinstance(endpoint, str) or not endpoint
        or any(character.isspace() or ord(character) < 32 or ord(character) == 127 for character in endpoint)
        or any(character in endpoint for character in ("\\", "?", "#"))
    ):
        raise invalid
    try:
        parts = urlsplit(endpoint)
        host = parts.hostname
        if (
            parts.scheme not in {"http", "https"}
            or host not in {"127.0.0.1", "::1", "localhost"}
            or parts.username is not None or parts.password is not None
            or parts.query or parts.fragment
        ):
            raise invalid
        port = parts.port
        if port is None:
            port = 443 if parts.scheme == "https" else 80
        if not 1 <= port <= 65535 or parts.netloc.endswith(":"):
            raise invalid
        path = parts.path.rstrip("/") + "/chat/completions"
        return Endpoint(urlunsplit((parts.scheme, parts.netloc, path, "", "")), host, port, parts.scheme)
    except ValueError:
        raise invalid from None


def validate_options(model: str, timeout: float, max_tokens: int, response_format: str) -> None:
    if not isinstance(model, str) or not model.strip():
        raise ModelClientError("model_options_invalid", "模型名不能为空。")
    if (
        isinstance(timeout, bool) or not isinstance(timeout, (int, float))
        or not math.isfinite(timeout) or not 0 < timeout <= 600
    ):
        raise ModelClientError("model_options_invalid", "超时必须大于 0 且不超过 600 秒。")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, int) or max_tokens <= 0:
        raise ModelClientError("model_options_invalid", "max_tokens 必须是正整数。")
    if response_format not in {"json_schema", "none"}:
        raise ModelClientError("model_options_invalid", "response_format 只能为 json_schema 或 none。")


def build_request(
    model: str, model_profile: dict[str, Any], issues: dict[str, Any],
    *, max_tokens: int = 4096, response_format: str = "json_schema",
) -> dict[str, Any]:
    validate_options(model, 120, max_tokens, response_format)
    user = {
        "operations": [item for item in operation_catalog() if item["op"] != "range_check"],
        "model_profile": model_profile,
        "issues": issues,
    }
    request: dict[str, Any] = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps(user, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)},
        ],
        "temperature": 0,
        "seed": 0,
        "max_tokens": max_tokens,
    }
    if response_format == "json_schema":
        # Copy the public schema, so callers cannot mutate subsequent requests.
        request["response_format"] = {
            "type": "json_schema",
            "json_schema": {
                "name": "cleaning_proposals",
                "schema": json.loads(json.dumps(PROPOSALS_SCHEMA)),
                "strict": True,
            },
        }
    return request


def _resolve_pointer(document: Any, pointer: str) -> Any:
    """Resolve RFC 6901 without accepting invalid escapes or array indices."""
    if pointer == "":
        return document
    if not pointer.startswith("/"):
        raise ValueError
    value = document
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(value, dict):
            value = value[token]
        elif isinstance(value, list):
            if re.fullmatch(r"0|[1-9][0-9]*", token) is None:
                raise ValueError
            value = value[int(token)]
        else:
            raise ValueError
    return value


def _verified_evidence_pointer(user: dict[str, Any], path: tuple[Any, ...], pointer: str) -> bool:
    if (
        len(path) != 6 or path[:2] != ("issues", "issues")
        or not isinstance(path[2], int) or path[3] != "evidence"
        or not isinstance(path[4], int) or path[5] != "pointer"
    ):
        return False
    try:
        evidence = user["issues"]["issues"][path[2]]["evidence"][path[4]]
        value = _resolve_pointer(user["model_profile"], pointer)
        # Evidence must match the actual JSON value; ledger canonicalization
        # would normalize Unicode and incorrectly merge distinct source text.
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False) == json.dumps(
            evidence["value"], ensure_ascii=False, sort_keys=True, allow_nan=False,
        )
    except (KeyError, IndexError, TypeError, ValueError, OverflowError):
        return False


def _decode_json(value: str | bytes) -> Any:
    return json.loads(value, object_pairs_hook=_unique_object, parse_constant=_reject_constant)


def encode_and_validate_request(
    request: dict[str, Any], *, input_filename: str,
    forbidden_paths: tuple[str, ...] = (),
) -> bytes:
    """Scan decoded keys and strings; JSON escaping is not user text.

    Only a verified issue-card evidence pointer may omit the file_path category.
    Its other sensitive categories and the referenced value are still checked.
    """
    try:
        body = json.dumps(
            request, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
        ).encode("utf-8")
        decoded = _decode_json(body)
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ModelClientError("model_request_invalid", "请求必须是有效的 JSON 数据。") from None
    if len(body) > MAX_REQUEST_BYTES:
        raise ModelClientError("model_request_too_large", "请求正文超过 256 KiB 限制。")
    forbidden = tuple(value for value in (input_filename, *forbidden_paths) if value)

    def check(text: str, exempt_pointer: bool = False) -> None:
        if any(value in text for value in forbidden):
            raise ModelClientError("model_request_sensitive", "请求包含输入文件名或本机路径，已拒绝发送。")
        categories = find_sensitive_types(text)
        if exempt_pointer:
            categories = [category for category in categories if category != "file_path"]
        if categories:
            raise ModelClientError("model_request_sensitive", "请求包含敏感内容，已拒绝发送。")

    def scan_user(value: Any, user: dict[str, Any], path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                check(key)
                scan_user(item, user, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                scan_user(item, user, path + (index,))
        elif isinstance(value, str):
            check(value, _verified_evidence_pointer(user, path, value))

    def scan(value: Any, path: tuple[Any, ...] = ()) -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                check(key)
                if (
                    len(path) == 2 and path[0] == "messages"
                    and isinstance(path[1], int) and value.get("role") == "user" and key == "content"
                ):
                    try:
                        user = _decode_json(item)
                        if not isinstance(user, dict):
                            raise ValueError
                    except (TypeError, ValueError, RecursionError):
                        raise ModelClientError("model_request_invalid", "用户消息必须包含完整的 JSON 对象。") from None
                    scan_user(user, user)
                else:
                    scan(item, path + (key,))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                scan(item, path + (index,))
        elif isinstance(value, str):
            check(value)

    try:
        scan(decoded)
    except RecursionError:
        raise ModelClientError("model_request_invalid", "请求 JSON 嵌套过深。") from None
    return body


class _NoRedirectHandler(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def post_request(endpoint: Endpoint, body: bytes, *, timeout: float) -> TransportResult:
    """POST once with no proxy, authorization, redirects, or retry."""
    started = time.monotonic()
    status: int | None = None
    received: bytes | None = None

    def result(
        code: str | None = None, message: str = "", *, complete: bool = True,
    ) -> TransportResult:
        return TransportResult(
            status, received, round((time.monotonic() - started) * 1000, 3), code, message, complete,
        )

    request = Request(
        endpoint.url, data=body, method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", "Accept": "application/json"},
    )
    opener = build_opener(ProxyHandler({}), _NoRedirectHandler())
    try:
        try:
            response = opener.open(request, timeout=timeout)
        except HTTPError as error:
            response = error
        with response:
            status = response.code
            chunks: list[bytes] = []
            length = 0
            while length <= MAX_RESPONSE_BYTES:
                chunk = response.read(min(65536, MAX_RESPONSE_BYTES + 1 - length))
                if not chunk:
                    break
                chunks.append(chunk)
                length += len(chunk)
                received = b"".join(chunks)
            if received is None:
                received = b""
        if len(received) > MAX_RESPONSE_BYTES:
            received = received[:MAX_RESPONSE_BYTES]
            return result("model_response_too_large", "模型响应超过 1 MiB 限制，仅保留前缀。", complete=False)
        if 300 <= status < 400:
            return result("model_redirect_rejected", "本机模型返回跳转，已拒绝跟随。")
        if status != 200:
            return result("model_http_error", "本机模型返回非 200 HTTP 状态。")
        return result()
    except (TimeoutError, socket.timeout):
        return result("model_timeout", "本机模型请求超时。", complete=False)
    except URLError as error:
        if isinstance(error.reason, (TimeoutError, socket.timeout)):
            return result("model_timeout", "本机模型请求超时。", complete=False)
        return result("model_connection_error", "无法连接本机模型服务。", complete=False)
    except (OSError, http.client.HTTPException, ValueError):
        return result("model_connection_error", "本机模型连接中断或响应传输失败。", complete=False)
