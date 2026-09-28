"""Tests for the parent-side egress proxy controller."""

from __future__ import annotations

from pathlib import Path

import pytest

import omnigent.inner.egress.controller as controller


class _FakeProxy:
    """Small proxy stand-in that records which listener was requested."""

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        self.port = 0
        self.started_tcp = False
        self.started_unix = False
        self.stopped = False

    async def start_tcp(self) -> int:
        self.started_tcp = True
        self.port = 43123
        return self.port

    async def start_unix(self, _path: str) -> None:
        self.started_unix = True

    async def stop(self) -> None:
        self.stopped = True


def test_direct_controller_keeps_tcp_listener_open(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ca = tmp_path / "ca.pem"
    key = tmp_path / "ca-key.pem"
    bundle = tmp_path / "bundle.pem"
    for path in (ca, key, bundle):
        path.write_text("placeholder", encoding="utf-8")
    monkeypatch.setattr(controller, "ensure_ca", lambda: (ca, key))
    monkeypatch.setattr(controller, "ensure_ca_bundle", lambda _ca: bundle)
    proxy = _FakeProxy()
    monkeypatch.setattr(controller, "EgressProxy", lambda *a, **k: proxy)

    handle = controller.start_egress_proxy(
        rules=("GET github.com/**",),
        tmpdir=tmp_path,
        allow_private_destinations=False,
        require_auth=True,
        direct=True,
    )
    try:
        assert handle.relay_port == 43123
        assert proxy.started_tcp is True
        assert proxy.started_unix is False
        assert handle.auth_token is not None
    finally:
        handle.stop()

    assert proxy.stopped is True
