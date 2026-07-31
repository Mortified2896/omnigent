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
            status, _body = _http_get(f"http://127.0.0.1:{port}/missing")
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


def test_canary_subprocess_env_sandboxed_harness_tmp(tmp_path: Path) -> None:
    """``_spawn_canary`` writes the harness tmp dir inside the release dir.

    Regression for the deploy gate's ``PermissionError: '/tmp/omnigent/ap-.../AP_PID'``
    that occurred when the updater-invoked canary process tried to
    ``stat()`` the per-AP sentinel file owned by ``hermes``. The canary
    now sets ``OMNIGENT_HARNESS_TMP_PARENT`` to a release-local path so
    the harness writes its sentinel into a directory owned by the
    invoking user.
    """
    from unittest.mock import patch

    from omnigent.deploy.supervisor import canary as canary_mod

    captured: dict[str, object] = {}

    class _FakePopen:
        def __init__(self, *args, **kwargs):
            captured["env"] = kwargs.get("env")
            captured["cmd"] = args[0] if args else kwargs.get("args")
            self.pid = 0

    with patch.object(canary_mod.subprocess, "Popen", _FakePopen):
        canary_mod._spawn_canary(
            tmp_path,
            port=12345,
            log_path=tmp_path / "log.txt",
            skip_web_ui=True,
            config=None,
        )
    env = captured["env"]
    assert env is not None
    expected_tmp_parent = tmp_path / "canary" / "harness-tmp"
    assert env["OMNIGENT_HARNESS_TMP_PARENT"] == str(expected_tmp_parent), (
        f"Canary subprocess should sandbox the harness tmp dir under "
        f"{expected_tmp_parent}; got {env['OMNIGENT_HARNESS_TMP_PARENT']!r}"
    )
