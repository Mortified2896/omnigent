"""Tests for ``_opencode_native_launch_config`` in ``omnigent/runner/app.py``.

The runner fetches a session snapshot over HTTP and validates it before
launching a runner-owned OpenCode terminal. Two regressions are guarded:

* The dataclass must surface the ``route_approval_enabled`` flag as
  ``omniroute_route_expected`` so the OpenCode startup path can decide
  between silent fallback (no approval) and strict validation (approval
  enabled). A missing field used to raise ``AttributeError`` deep inside
  the ``fetch_omniroute_combo_models`` except-arm and bubble up to the UI
  as the generic ``native_terminal_start_failed`` banner.
* The runner-side ``fetch_omniroute_combo_models`` helper must accept the
  same shared bearer token the backend uses (``OMNIGENT_ROUTER_API_KEY``)
  in addition to the canonical ``OMNIGENT_OMNIROUTE_API_KEY`` /
  ``OMNIROUTE_API_KEY`` aliases, so a host runner with the same secret
  source can reach ``/v1/models`` when populating the OpenCode catalog.

These tests also exercise invalid snapshots the same way the existing
``test_codex_native_launch_config.py`` tests do, to keep the contract
symmetric.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.opencode_native_provider import fetch_omniroute_combo_models
from omnigent.runner.app import _opencode_native_launch_config


class _Resp:
    """Minimal stand-in for an httpx response carrying a fixed status + payload.

    Provides the tiny slice ``fetch_omniroute_combo_models`` actually
    uses: ``status_code``, ``json()``, and ``raise_for_status()``. Keeps the
    rest of the test surface ordinary Python so we can plug a stub into
    the helper's module-level ``httpx.AsyncClient`` reference without
    importing the real httpx testsuite machinery.
    """

    def __init__(
        self,
        status_code: int,
        payload: Any,
        *,
        json_raises: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload

    def raise_for_status(self) -> None:
        """Raise ``OpenCodeOmniRouteConfigurationError`` on non-2xx codes.

        Mirrors ``httpx.Response.raise_for_status`` for the slice we use;
        raises the same exception the production code raises, so the test
        verifies the real error path rather than just ``HTTPError``.
        """
        if self.status_code >= 400:
            from omnigent.opencode_native_provider import OpenCodeOmniRouteConfigurationError

            raise OpenCodeOmniRouteConfigurationError(
                f"fetch_omniroute helper observed status {self.status_code}"
            )


class _Client:
    """Async client stub whose ``get`` returns a fixed response or raises."""

    def __init__(
        self,
        resp: _Resp | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self._resp = resp
        self._raise_exc = raise_exc

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._resp is not None
        return self._resp


async def _run(
    client: _Client | None,
    session_id: str = "conv_1",
) -> Any:
    return await _opencode_native_launch_config(
        session_id=session_id, server_client=client
    )


@pytest.mark.asyncio
async def test_missing_client_raises() -> None:
    """No server client means there is no way to fetch config — fail loud."""
    with pytest.raises(RuntimeError, match="server_client is required"):
        await _run(None)


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """A transport error fetching the snapshot surfaces as a RuntimeError."""
    client = _Client(raise_exc=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="Could not fetch OpenCode launch config"):
        await _run(client)


@pytest.mark.asyncio
async def test_non_200_raises() -> None:
    """A non-200 status is rejected and names the status in the error."""
    client = _Client(_Resp(404, None))
    with pytest.raises(RuntimeError, match="returned 404"):
        await _run(client)


@pytest.mark.asyncio
async def test_invalid_json_raises() -> None:
    """A body that does not parse as JSON is rejected."""
    client = _Client(_Resp(200, None, json_raises=True))
    with pytest.raises(RuntimeError, match="invalid JSON"):
        await _run(client)


@pytest.mark.asyncio
async def test_non_dict_snapshot_raises() -> None:
    """A JSON array (not an object) is not a valid session snapshot."""
    client = _Client(_Resp(200, ["not", "a", "dict"]))
    with pytest.raises(RuntimeError, match="not a JSON object"):
        await _run(client)


@pytest.mark.asyncio
async def test_route_approval_enabled_surfaces_as_omniroute_route_expected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``route_approval_enabled=True`` must round-trip into the launch config.

    Regression: a previous merge dropped the dataclass field even though
    ``_auto_create_opencode_terminal`` reads ``launch_config.omniroute_route_expected``
    in four places. Re-asserting the field exists here means the failure
    surfaces at construction time (where it is recoverable) rather than
    deep inside the catalog-fetch except-arm (where it was masked as
    ``native_terminal_start_failed``).
    """
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "route_approval_enabled": True,
        "omniroute_route_id": "auto/coding",
    }
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.omniroute_route_expected is True, (
        "route_approval_enabled must round-trip into omniroute_route_expected."
    )
    # The approved route id is the only model_override that survives — the
    # runner qualifies it (omniroute/<id>) once the catalog is loaded.
    assert cfg.model_override == "auto/coding"


@pytest.mark.asyncio
async def test_route_approval_disabled_default_omniroute_route_expected_false(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``route_approval_enabled`` absent or False must default to False.

    Keep the safe default. A session that didn't opt into route approval
    must never be re-pinned to the OmniRoute catalog at start time.
    """
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", "/tmp/repo")
    for snapshot in (
        {},
        {"route_approval_enabled": False},
        {"route_approval_enabled": None},
    ):
        cfg = await _run(_Client(_Resp(200, snapshot)))
        assert cfg.omniroute_route_expected is False, (
            f"Default for {snapshot!r} must be False, got {cfg.omniroute_route_expected!r}."
        )


@pytest.mark.asyncio
async def test_invalid_field_raises_invalid_permission_mode(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Malformed permission_mode must be rejected before OpenCode boots."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    client = _Client(_Resp(200, {"permission_mode": 0}))
    with pytest.raises(RuntimeError, match="Invalid permission_mode"):
        await _run(client)


@pytest.mark.asyncio
async def test_happy_path_parses_full_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A well-formed snapshot (with fork labels) parses into a launch config."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "terminal_launch_args": ["--no-telemetry"],
        "model_override": "anthropic/claude-sonnet-4-5",
        "reasoning_effort": "medium",
        "permission_mode": "accept-edits",
        "external_session_id": "sess_xyz",
        "route_approval_enabled": False,
        "labels": {"omnigent.fork.carry_history": "1"},
    }
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.policy_server_url == "http://127.0.0.1:8123"
    assert cfg.terminal_launch_args == ["--no-telemetry"]
    assert cfg.model_override == "anthropic/claude-sonnet-4-5"
    assert cfg.reasoning_effort == "medium"
    assert cfg.permission_mode == "accept-edits"
    assert cfg.external_session_id == "sess_xyz"
    assert cfg.omniroute_route_expected is False
    assert cfg.fork_carry_history is True
    assert cfg.workspace.name == "repo"


# ---------------------------------------------------------------------------
# ``fetch_omniroute_combo_models`` accepts the shared omniroute bearer token
# ---------------------------------------------------------------------------


class _FakeAsyncClient:
    """Capture the request the function makes and return a canned response.

    Mirrors only what the real ``httpx.AsyncClient`` exposes to the helper:
    a context-manager protocol, ``get``, plus ``raise_for_status``. The last
    request is recorded on the instance so subclasses can override ``get``
    without losing the visibility the assertions need.
    """

    def __init__(
        self,
        *,
        json_payload: dict[str, Any] | None = None,
        status_code: int = 200,
    ) -> None:
        self._json_payload = json_payload or {"data": []}
        self._status_code = status_code
        self.last_request_headers: dict[str, str] | None = None
        self.last_request_url: str | None = None

    async def __aenter__(self) -> _FakeAsyncClient:
        return self

    async def __aexit__(self, *_exc_info: Any) -> None:
        return None

    async def get(self, url: str, headers: dict[str, str] | None = None) -> _Resp:
        self.last_request_headers = headers or {}
        self.last_request_url = url
        return _Resp(self._status_code, self._json_payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "env_var",
    [
        "OMNIGENT_OMNIROUTE_API_KEY",
        "OMNIGENT_ROUTER_API_KEY",
        "OMNIROUTE_API_KEY",
    ],
)
async def test_fetch_omniroute_combo_models_picks_up_shared_bearer(
    monkeypatch: pytest.MonkeyPatch,
    env_var: str,
) -> None:
    """Each documented env var forwards as a Bearer token to /v1/models.

    Regression: the helper used to read only ``OMNIGENT_OMNIROUTE_API_KEY``
    and ``OMNIROUTE_API_KEY``. The host runner is wired through
    ``/etc/omnigent/router.env`` which sets ``OMNIGENT_ROUTER_API_KEY`` —
    that env-var name must also authenticate, otherwise the OpenCode
    startup silently hits /v1/models with no Authorization header and
    OmniRoute returns 401, which (before the dataclass fix above) used to
    surface as ``native_terminal_start_failed``.
    """
    for var in (
        "OMNIGENT_OMNIROUTE_API_KEY",
        "OMNIGENT_ROUTER_API_KEY",
        "OMNIROUTE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv(env_var, "shared-bearer-token")
    captured: _FakeAsyncClient | None = None

    def _factory(*, timeout: float) -> _FakeAsyncClient:
        nonlocal captured
        captured = _FakeAsyncClient()
        return captured

    monkeypatch.setattr(
        "omnigent.opencode_native_provider.httpx.AsyncClient",
        _factory,
    )

    combos = await fetch_omniroute_combo_models()

    assert combos == {}
    assert captured is not None
    assert captured.last_request_headers is not None
    assert captured.last_request_headers["Authorization"] == (
        "Bearer shared-bearer-token"
    ), (
        f"{env_var} must authenticate the runner-side /v1/models call."
    )


@pytest.mark.asyncio
async def test_fetch_omniroute_combo_models_unauthorized_when_no_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without any bearer env var, the helper passes no Authorization header.

    The 401 response then surfaces as
    ``OpenCodeOmniRouteConfigurationError`` (no leaked credentials).
    """
    for var in (
        "OMNIGENT_OMNIROUTE_API_KEY",
        "OMNIGENT_ROUTER_API_KEY",
        "OMNIROUTE_API_KEY",
    ):
        monkeypatch.delenv(var, raising=False)
    captured: _FakeAsyncClient | None = None

    def _factory(*, timeout: float) -> _FakeAsyncClient:
        nonlocal captured
        captured = _FakeAsyncClient(status_code=401, json_payload={"error": "unauthorized"})
        return captured

    monkeypatch.setattr(
        "omnigent.opencode_native_provider.httpx.AsyncClient",
        _factory,
    )

    from omnigent.opencode_native_provider import OpenCodeOmniRouteConfigurationError

    with pytest.raises(OpenCodeOmniRouteConfigurationError):
        await fetch_omniroute_combo_models()
    assert captured is not None
    assert captured.last_request_headers == {}, (
        "No env-var bearer must mean no Authorization header was sent."
    )
