"""A bounded, disposable loopback HTTP service for synthetic model tests."""
from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class FakeModelServer:
    """Record requests and serve bytes, JSON, or a response callback."""

    def __init__(self, body, status=200, delay=0, headers=None):
        self.body = body
        self.status = status
        self.delay = delay
        self.headers = dict(headers or {})
        self.requests = []

    def __enter__(self):
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                request = {
                    "path": self.path,
                    "headers": dict(self.headers.items()),
                    "body": self.rfile.read(int(self.headers.get("Content-Length", "0"))),
                }
                owner.requests.append(request)
                if owner.delay:
                    time.sleep(owner.delay)
                body = owner.body(request) if callable(owner.body) else owner.body
                if isinstance(body, dict):
                    body = json.dumps(body, ensure_ascii=False).encode("utf-8")
                if isinstance(body, str):
                    body = body.encode("utf-8")
                try:
                    self.send_response(owner.status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    for key, value in owner.headers.items():
                        self.send_header(key, value)
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

            def log_message(self, format, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = False
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}/v1"
        self.thread = threading.Thread(
            target=self.server.serve_forever, kwargs={"poll_interval": 0.01},
            daemon=True,
        )
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def completion(proposals=None, *, content=None, finish_reason="stop"):
    """Return a hand-written OpenAI-compatible synthetic response."""
    if content is None:
        content = json.dumps({"proposals": proposals or []}, ensure_ascii=False)
    return {
        "id": "synthetic-completion",
        "model": "synthetic-reply-model",
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": content},
            "finish_reason": finish_reason,
        }],
        "usage": {"prompt_tokens": 12, "completion_tokens": 7, "total_tokens": 19},
    }
