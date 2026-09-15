"""Opt-in loopback transport for per-request gateway provenance.

Authentication is forwarded only to the configured gateway and never recorded.
Gateway execution evidence is read from the colocated private recorder spool.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from contextlib import suppress
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx

_ID = re.compile(r"^[a-zA-Z0-9_.:/@+-]{1,160}$")
_USAGE = {
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "prompt_tokens",
    "completion_tokens",
    "cached_tokens",
    "reasoning_tokens",
}
_HOP = {
    "connection",
    "transfer-encoding",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "upgrade",
}


def _identifier(value: Any) -> str | None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        return None
    if value.startswith(("sk-", "ghp_", "eyJ")):
        return None
    return value


def read_gateway_evidence(root: Path, call_id: str) -> dict[str, Any] | None:
    """Accept only bounded gateway-owned identifiers, metrics and statuses."""
    try:
        uuid.UUID(call_id)
        path = root / ".gateway" / (call_id + ".json")
        if path.is_symlink() or path.stat().st_size > 256 * 1024:
            return None
        data = json.loads(path.read_text())
        if (
            data.get("gateway_call_id") != call_id
            or data.get("evidence_source") != "omniroute_executor_fetch"
        ):
            return None
        request_id = str(uuid.UUID(data["omniroute_request_id"]))
        attempts = data.get("attempts")
        if not isinstance(attempts, list) or len(attempts) > 64:
            return None
        clean = []
        for index, row in enumerate(attempts):
            if row.get("attempt_index") != index:
                return None
            attempt: dict[str, Any] = {
                "attempt_index": index,
                "attempt_id": str(uuid.UUID(row["attempt_id"])),
            }
            for key in (
                "provider",
                "canonical_model",
                "model_evidence",
                "safe_connection_id",
                "route",
                "selection_strategy",
                "retry_reason",
                "fallback_reason",
                "previous_attempt_id",
                "terminal_status",
                "started_at",
                "completed_at",
            ):
                attempt[key] = _identifier(row.get(key))
            for key in ("http_status", "duration_ms"):
                value = row.get(key)
                attempt[key] = (
                    value
                    if isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and value >= 0
                    else None
                )
            usage = row.get("usage")
            attempt["usage"] = (
                {
                    k: v
                    for k, v in usage.items()
                    if k in _USAGE
                    and isinstance(v, (int, float))
                    and not isinstance(v, bool)
                    and v >= 0
                }
                if isinstance(usage, dict)
                else None
            )
            clean.append(attempt)
        return {
            "evidence_source": "omniroute_executor_fetch",
            "gateway_call_id": call_id,
            "omniroute_request_id": request_id,
            "upstream_correlation_id": _identifier(data.get("upstream_correlation_id")),
            "attempts": clean,
            "errors": [_identifier(e) for e in data.get("errors", [])[:16] if _identifier(e)],
        }
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


class GatewayCaptureProxy:
    """A session-owned proxy; every HTTP request receives a fresh opaque ID."""

    def __init__(self, upstream: str) -> None:
        parsed = urlsplit(upstream)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("unsupported capture gateway URL")
        self.upstream = upstream.rstrip("/")
        self.prefix = "/" + uuid.uuid4().hex
        self.capture: Any = None
        self.requests: list[dict[str, Any]] = []
        self.lock = threading.Lock()
        owner = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, format: str, *args: Any) -> None:
                pass

            def do_GET(self) -> None:
                self.forward()

            def do_POST(self) -> None:
                self.forward()

            def forward(self) -> None:
                if not self.path.startswith(owner.prefix + "/"):
                    self.send_error(404)
                    return
                suffix = self.path[len(owner.prefix) :]
                if suffix.startswith("//") or ".." in suffix.split("/"):
                    self.send_error(400)
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    self.send_error(400)
                    return
                if not 0 <= size <= 32 * 1024 * 1024:
                    self.send_error(413)
                    return
                content = self.rfile.read(size)
                headers = {
                    k: v
                    for k, v in self.headers.items()
                    if k.lower()
                    not in _HOP
                    | {"host", "content-length", "x-omnigent-gateway-call-id", "x-request-id"}
                }
                headers["accept-encoding"] = "identity"
                row: dict[str, Any] | None = None
                with owner.lock:
                    if (
                        owner.capture is not None
                        and self.command == "POST"
                        and suffix.split("?")[0] in {"/responses", "/chat/completions"}
                    ):
                        call_id = str(uuid.uuid4())
                        headers["x-omnigent-gateway-call-id"] = call_id
                        headers["x-request-id"] = call_id
                        row = {
                            "gateway_call_id": call_id,
                            "http_status": None,
                            "omniroute_request_id": None,
                        }
                        owner.requests.append(row)
                try:
                    with (
                        httpx.Client(
                            timeout=httpx.Timeout(600, connect=15),
                            follow_redirects=False,
                            trust_env=False,
                        ) as client,
                        client.stream(
                            self.command, owner.upstream + suffix, headers=headers, content=content
                        ) as response,
                    ):
                        if row is not None:
                            row["http_status"] = response.status_code
                            value = response.headers.get("x-omniroute-capture-request-id")
                            with suppress(ValueError):
                                row["omniroute_request_id"] = (
                                    str(uuid.UUID(value)) if value else None
                                )
                        self.send_response(response.status_code)
                        for k, v in response.headers.items():
                            if k.lower() not in _HOP | {"content-length"}:
                                self.send_header(k, v)
                        self.end_headers()
                        for chunk in response.iter_raw():
                            self.wfile.write(chunk)
                            self.wfile.flush()
                except (httpx.HTTPError, OSError):
                    if row is not None:
                        row["transport_error"] = True
                    self.close_connection = True

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base_url = f"http://127.0.0.1:{self.server.server_port}{self.prefix}"

    def bind(self, capture: Any) -> None:
        with self.lock:
            self.capture = capture
            self.requests = []
            if capture is not None:
                capture.gateway_requests = self.requests

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
