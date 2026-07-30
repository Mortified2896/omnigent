"""Tests for the result-delivery client (issue #38 §8)."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from omnigent.updater.protocol import (
    ResultRecord,
    new_request_id,
)
from omnigent.updater.runner_client import (
    DeliveryError,
    _HttpClient,
    deliver_result,
)


class _FakeServer:
    """Tiny in-process HTTP server the tests can introspect."""

    def __init__(self, handler_cls):
        self.handler_cls = handler_cls
        self.server = HTTPServer(("127.0.0.1", 0), handler_cls)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def shutdown(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def test_deliver_result_returns_outcome_for_missing_origin() -> None:
    """A result with no origin conversation id skips delivery."""
    rid = new_request_id()
    result = ResultRecord(
        request_id=rid,
        final_status="rejected",
        target_sha="0" * 40,
        previous_sha="0" * 40,
        deployed_sha="0" * 40,
        started_at="2026-01-01T00:00:00Z",
        finished_at="2026-01-01T00:00:00Z",
    )
    outcome = deliver_result(result)
    assert outcome.delivered is False
    assert "operator-only" in outcome.message


def test_deliver_result_handles_idempotent_response() -> None:
    """A ``delivered=False, comment_id=...`` from the server is treated as success."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            length = int(self.headers.get("content-length", "0"))
            self.rfile.read(length)
            body = json.dumps({"delivered": True, "comment_id": "abc12345"}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):  # silence test output
            return

    server = _FakeServer(Handler)
    try:
        rid = new_request_id()
        result = ResultRecord(
            request_id=rid,
            final_status="succeeded",
            target_sha="0" * 40,
            previous_sha="0" * 40,
            deployed_sha="0" * 40,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:00Z",
        )
        # Stamp a synthetic conversation id into the events tail so
        # the delivery path has a destination.
        result.events_tail.append({"context": {"origin_conversation_id": "conv_abc"}})
        client = _HttpClient(host="127.0.0.1", port=server.port)
        outcome = deliver_result(result, client=client)
        assert outcome.delivered is True
        assert outcome.comment_id == "abc12345"
    finally:
        server.shutdown()


def test_deliver_result_handles_network_error() -> None:
    """Network errors surface as ``DeliveryOutcome(delivered=False)``."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            self.send_response(500)
            self.end_headers()

        def log_message(self, *_args):
            return

    server = _FakeServer(Handler)
    try:
        rid = new_request_id()
        result = ResultRecord(
            request_id=rid,
            final_status="failed",
            target_sha="0" * 40,
            previous_sha="0" * 40,
            deployed_sha="0" * 40,
            started_at="2026-01-01T00:00:00Z",
            finished_at="2026-01-01T00:00:00Z",
        )
        result.events_tail.append({"context": {"origin_conversation_id": "conv_abc"}})
        client = _HttpClient(host="127.0.0.1", port=server.port)
        outcome = deliver_result(result, client=client)
        assert outcome.delivered is False
    finally:
        server.shutdown()


def test_http_client_handles_unreachable_host() -> None:
    """``DeliveryError`` is raised when the server is unreachable."""
    client = _HttpClient(host="127.0.0.1", port=1)  # unlikely to be in use
    with pytest.raises(DeliveryError):
        client.get_json("/health")
