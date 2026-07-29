"""Tests for the canary probe module.

We don't actually fork a uvicorn server in unit tests — the canary is
exercised end-to-end by the live migration rehearsal. These tests pin
the pure-Python logic (port-picking, asset extraction, HTTP helper
behavior) so a regression in the helper layer is caught in CI.
"""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from omnigent.deploy.supervisor.canary import (
    CanaryError,
    _extract_index_assets,
    _http_get,
    _pick_free_port,
    _wait_for_bind,
)


def test_pick_free_port_returns_unique_ports() -> None:
    """Each call gives a different ephemeral port."""
    a = _pick_free_port()
    b = _pick_free_port()
    assert a != b
    assert 1024 < a < 65535
    assert 1024 < b < 65535


def test_wait_for_bind_returns_false_on_timeout() -> None:
    """An unreachable port returns False within the timeout."""
    # Pick a port that's not bound (then close).
    port = _pick_free_port()
    # Don't bind anything to it; wait_for_bind should time out.
    assert _wait_for_bind(port, timeout_s=0.2) is False


def test_wait_for_bind_returns_true_when_bound() -> None:
    """A bound port returns True."""
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        port = int(server.getsockname()[1])
        assert _wait_for_bind(port, timeout_s=2.0) is True
    finally:
        server.close()


def test_http_get_handles_connection_refused(tmp_path: Path) -> None:
    """HTTP GET against an unbound port raises CanaryError."""
    port = _pick_free_port()
    with pytest.raises(CanaryError):
        _http_get(f"http://127.0.0.1:{port}/")


def test_http_get_returns_404_body(tmp_path: Path) -> None:
    """A 404 response is surfaced as a (404, '') tuple without raising."""
    import http.server
    import socketserver
    import threading

    port = _pick_free_port()

    class _Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(404)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"")

        def log_message(self, format: str, *args: object) -> None:
            return

    with socketserver.TCPServer(("127.0.0.1", port), _Handler) as httpd:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        try:
            status, body = _http_get(f"http://127.0.0.1:{port}/missing")
            assert status == 404
        finally:
            httpd.shutdown()


def test_extract_index_assets_returns_referenced_scripts() -> None:
    """Script tags and link hrefs are pulled out for reachability probes."""
    html = """
    <html><head>
      <link rel="stylesheet" href="/assets/index-abc.css">
      <link rel="stylesheet" href="/assets/index-def.css">
    </head><body>
      <script type="module" src="/assets/index-abc.js"></script>
      <script src="/assets/runtime-def.js"></script>
    </body></html>
    """
    # The extractor iterates script tags first, then link[rel=stylesheet],
    # so the expected order is scripts then stylesheets.
    assert _extract_index_assets(html) == [
        "/assets/index-abc.js",
        "/assets/runtime-def.js",
        "/assets/index-abc.css",
        "/assets/index-def.css",
    ]


def test_extract_index_assets_handles_empty() -> None:
    """An empty document returns an empty list, not a crash."""
    assert _extract_index_assets("") == []


def test_extract_index_assets_caps_at_twenty() -> None:
    """At most 20 assets are returned so a buggy probe can't run forever."""
    refs = "".join(f'<script src="/assets/{i}.js"></script>' for i in range(50))
    assert len(_extract_index_assets(refs)) == 20
