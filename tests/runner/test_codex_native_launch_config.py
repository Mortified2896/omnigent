"""Tests for ``_codex_native_launch_config`` in ``omnigent/runner/app.py``.

The runner fetches a session snapshot over HTTP and validates it before
launching a runner-owned Codex terminal. Each malformed field is meant to
fail loud with a RuntimeError rather than launch Codex with garbage; those
guards were previously unexercised by any direct test. These tests drive the
function with a stub async client returning controlled snapshots.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from omnigent.model_advisor_provider_policy import LogicalChoice
from omnigent.runner.app import _codex_native_launch_config


class _Resp:
    """Minimal stand-in for an httpx response carrying a fixed status + payload."""

    def __init__(self, status_code: int, payload: Any, *, json_raises: bool = False) -> None:
        self.status_code = status_code
        self._payload = payload
        self._json_raises = json_raises

    def json(self) -> Any:
        if self._json_raises:
            raise ValueError("not json")
        return self._payload


class _Client:
    """Async client stub whose ``get`` returns a fixed response or raises."""

    def __init__(self, resp: _Resp | None = None, raise_exc: Exception | None = None) -> None:
        self._resp = resp
        self._raise_exc = raise_exc

    async def get(self, url: str, timeout: float | None = None) -> _Resp:
        if self._raise_exc is not None:
            raise self._raise_exc
        assert self._resp is not None
        return self._resp


async def _run(client: _Client | None, session_id: str = "conv_1") -> Any:
    return await _codex_native_launch_config(session_id=session_id, server_client=client)


@pytest.mark.asyncio
async def test_missing_client_raises() -> None:
    """No server client means there is no way to fetch config — fail loud."""
    with pytest.raises(RuntimeError, match="server_client is required"):
        await _run(None)


@pytest.mark.asyncio
async def test_http_error_raises() -> None:
    """A transport error fetching the snapshot surfaces as a RuntimeError."""
    client = _Client(raise_exc=httpx.ConnectError("boom"))
    with pytest.raises(RuntimeError, match="Could not fetch Codex launch config"):
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
@pytest.mark.parametrize(
    ("field", "value", "match"),
    [
        ("terminal_launch_args", "not-a-list", "terminal_launch_args"),
        ("terminal_launch_args", [1, 2], "terminal_launch_args"),
        ("model_override", "", "model_override"),
        ("model_override", 5, "model_override"),
        ("reasoning_effort", "bogus", "reasoning_effort"),
        ("reasoning_effort", 5, "reasoning_effort"),
        ("external_session_id", "", "external_session_id"),
        ("workspace", "", "workspace"),
    ],
)
async def test_invalid_field_raises(field: str, value: Any, match: str) -> None:
    """Each malformed optional field is rejected with a field-specific error."""
    client = _Client(_Resp(200, {field: value}))
    with pytest.raises(RuntimeError, match=match):
        await _run(client)


@pytest.mark.asyncio
async def test_happy_path_parses_full_config(monkeypatch: pytest.MonkeyPatch) -> None:
    """A well-formed snapshot (with fork labels) parses into a launch config."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "terminal_launch_args": ["--config", "approval_policy=on-request"],
        "model_override": "gpt-5.4-mini",
        "reasoning_effort": "low",
        "external_session_id": "thread_abc",
        "labels": {
            "omnigent.fork.source_id": "conv_source",
            "omnigent.fork.source_external_session_id": "thread_src",
            "omnigent.fork.carry_history": "1",
            "omnigent.harnesses.codex_native.main.bypass_sandbox": "1",
            "omnigent.access_lane": "codex-direct",
        },
    }
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.policy_server_url == "http://127.0.0.1:8123"
    assert cfg.terminal_launch_args == ["--config", "approval_policy=on-request"]
    assert cfg.model_override == "gpt-5.4-mini"
    assert cfg.reasoning_effort == "low"
    assert cfg.access_lane == "codex-direct"
    assert cfg.external_session_id == "thread_abc"
    assert cfg.fork_source_id == "conv_source", "Fork source id should be read from labels."
    assert cfg.fork_source_external_id == "thread_src"
    assert cfg.fork_carry_history is True, "carry_history label '1' should parse to True."
    assert cfg.bypass_sandbox is True, "bypass_sandbox label '1' should parse to True."
    assert cfg.workspace.name == "repo", (
        f"Workspace path should resolve from snapshot, got {cfg.workspace}."
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "labels",
    [
        None,  # no labels at all
        {},  # labels present but no bypass key
        {"omnigent.harnesses.codex_native.main.bypass_sandbox": "0"},  # explicit off
        {"omnigent.harnesses.codex_native.main.bypass_sandbox": "true"},  # only "1" arms it
        {"omnigent.harnesses.codex_native.main.bypass_sandbox": ""},  # empty string
    ],
)
async def test_bypass_sandbox_defaults_off_unless_label_is_one(
    monkeypatch: pytest.MonkeyPatch, labels: Any
) -> None:
    """
    Fail-safe: ``bypass_sandbox`` is False unless the label is exactly ``"1"``.

    The dangerous full-bypass stance must never be entered by accident, so an
    absent label, an unrelated value, or any near-miss (``"0"``, ``"true"``,
    ``""``) leaves Codex in its normal approval/sandbox stance. Only the
    canonical ``"1"`` (set by the guarded web toggle) arms it.
    """
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    monkeypatch.delenv("OMNIGENT_CODEX_NATIVE_TRUSTED", raising=False)
    snapshot: dict[str, Any] = {"workspace": "/tmp/repo"}
    if labels is not None:
        snapshot["labels"] = labels
    cfg = await _run(_Client(_Resp(200, snapshot)))
    assert cfg.bypass_sandbox is False
    assert cfg.reasoning_effort is None
    assert cfg.access_lane is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("deployment_value", "label_value", "expected"),
    [
        ("false", None, False),
        ("0", None, False),
        ("unexpected", None, False),
        ("true", None, True),
        (" YES ", None, True),
        ("on", "0", True),
        ("false", "1", True),
        ("true", "1", True),
    ],
)
async def test_deployment_trust_composes_with_session_label(
    monkeypatch: pytest.MonkeyPatch,
    deployment_value: str,
    label_value: str | None,
    expected: bool,
) -> None:
    """Deployment trust is explicit, fail-closed, and ORed with the label."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    monkeypatch.setenv("OMNIGENT_CODEX_NATIVE_TRUSTED", deployment_value)
    labels = {"omnigent.access_lane": "codex-direct"}
    if label_value is not None:
        labels["omnigent.harnesses.codex_native.main.bypass_sandbox"] = label_value
    snapshot: dict[str, Any] = {
        "workspace": "/tmp/repo",
        "model_override": "gpt-5.5",
        "reasoning_effort": "low",
        "labels": labels,
    }

    cfg = await _run(_Client(_Resp(200, snapshot)))

    assert cfg.bypass_sandbox is expected
    assert cfg.access_lane == "codex-direct"
    assert cfg.model_override == "gpt-5.5"
    assert cfg.reasoning_effort == "low"


@pytest.mark.asyncio
@pytest.mark.parametrize("access_lane", ["unknown", "", 42])
async def test_invalid_access_lane_fails_loud(
    monkeypatch: pytest.MonkeyPatch, access_lane: Any
) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "model_override": "gpt-5.5",
        "labels": {"omnigent.access_lane": access_lane},
    }
    with pytest.raises(RuntimeError, match="Invalid native Codex access lane"):
        await _run(_Client(_Resp(200, snapshot)))


@pytest.mark.asyncio
async def test_access_lane_requires_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "labels": {"omnigent.access_lane": "omniroute"},
    }
    with pytest.raises(RuntimeError, match="requires model_override"):
        await _run(_Client(_Resp(200, snapshot)))


@pytest.mark.asyncio
async def test_v2_advisor_binding_is_checked_at_native_launch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A v2 session cannot drift from the server-attested route/account."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    choice = LogicalChoice("openai", "gpt-5.5", "low")
    route = {
        "choice": {
            "provider": choice.provider,
            "model_id": choice.model_id,
            "reasoning_effort": choice.reasoning_effort,
        },
        "transport": "omniroute",
        "route_id": "omniroute",
        "wire_model": "gpt-5.5",
        "wire_effort": "low",
        "entitlement_kind": "chatgpt_plan",
        "entitlement_key": "chatgpt-plan:rtx-codex-owner",
        "equivalence_key": "openai:codex-native:responses:gpt-5.5:low",
        "catalog_revision": "catalog-v1",
        "ready": True,
        "connection_id": "omniroute-codex-oauth",
    }
    plan = {
        "choice": route["choice"],
        "preference": "omniroute_preferred",
        "primary": route,
        "fallback": None,
        "reason": "omniroute_preferred",
    }
    snapshot = {
        "workspace": "/tmp/repo",
        "model_override": "gpt-5.5",
        "reasoning_effort": "low",
        "labels": {
            "omnigent.advisor.round_id": "adviseround-v2",
            "omnigent.advisor.logical_choice_id": choice.choice_id,
            "omnigent.advisor.transport_plan": json.dumps(plan),
            "omnigent.advisor.dispatch_route": json.dumps(route),
            "omnigent.advisor.connection_id": "omniroute-codex-oauth",
            "omnigent.access_lane": "omniroute",
        },
    }

    cfg = await _run(_Client(_Resp(200, snapshot)))

    assert cfg.access_lane == "omniroute"
    assert cfg.advisor_connection_id == "omniroute-codex-oauth"


@pytest.mark.asyncio
async def test_v2_advisor_binding_rejects_wrong_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    choice = LogicalChoice("openai", "gpt-5.5", "low")
    route = {
        "choice": {
            "provider": choice.provider,
            "model_id": choice.model_id,
            "reasoning_effort": choice.reasoning_effort,
        },
        "transport": "omniroute",
        "route_id": "omniroute",
        "wire_model": "gpt-5.5",
        "wire_effort": "low",
        "entitlement_kind": "chatgpt_plan",
        "entitlement_key": "chatgpt-plan:rtx-codex-owner",
        "equivalence_key": "openai:codex-native:responses:gpt-5.5:low",
        "catalog_revision": "catalog-v1",
        "ready": True,
        "connection_id": "omniroute-glm-coding-plan",
    }
    plan = {
        "choice": route["choice"],
        "preference": "omniroute_preferred",
        "primary": route,
        "fallback": None,
        "reason": "omniroute_preferred",
    }
    snapshot = {
        "workspace": "/tmp/repo",
        "model_override": "gpt-5.5",
        "reasoning_effort": "low",
        "labels": {
            "omnigent.advisor.round_id": "adviseround-v2",
            "omnigent.advisor.logical_choice_id": choice.choice_id,
            "omnigent.advisor.transport_plan": json.dumps(plan),
            "omnigent.advisor.dispatch_route": json.dumps(route),
            "omnigent.advisor.connection_id": "omniroute-glm-coding-plan",
            "omnigent.access_lane": "omniroute",
        },
    }

    with pytest.raises(RuntimeError, match="not qualified"):
        await _run(_Client(_Resp(200, snapshot)))


@pytest.mark.asyncio
async def test_v2_advisor_binding_rejects_partial_server_labels(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "model_override": "gpt-5.5",
        "labels": {
            "omnigent.advisor.round_id": "adviseround-v2",
            "omnigent.advisor.logical_choice_id": "logical-not-enough",
            "omnigent.access_lane": "omniroute",
        },
    }

    with pytest.raises(RuntimeError, match="incomplete transport binding"):
        await _run(_Client(_Resp(200, snapshot)))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("model_override", "spelling"),
    [
        pytest.param("glm-5.3", "provider-local", id="provider-local"),
        pytest.param("glm/glm-5.3", "legacy-qualified", id="legacy-qualified"),
    ],
)
async def test_glm_direct_lane_is_an_allowed_persisted_choice(
    monkeypatch: pytest.MonkeyPatch, model_override: str, spelling: str
) -> None:
    """A stored glm-direct lane survives the snapshot round-trip in both id
    spellings the direct lane accepts: the provider-local id and the legacy
    OmniRoute-qualified alias for the same model."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")
    snapshot = {
        "workspace": "/tmp/repo",
        "model_override": model_override,
        "labels": {"omnigent.access_lane": "glm-direct"},
    }

    cfg = await _run(_Client(_Resp(200, snapshot)))

    assert cfg.model_override == model_override
    assert cfg.access_lane == "glm-direct"


@pytest.mark.asyncio
async def test_omniroute_and_glm_direct_lanes_stay_distinct_on_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Reloading the same model under different lanes yields different lanes."""
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://127.0.0.1:8123")

    omniroute_cfg = await _run(
        _Client(
            _Resp(
                200,
                {
                    "workspace": "/tmp/repo",
                    "model_override": "glm/glm-5.3",
                    "labels": {"omnigent.access_lane": "omniroute"},
                },
            )
        )
    )
    direct_cfg = await _run(
        _Client(
            _Resp(
                200,
                {
                    "workspace": "/tmp/repo",
                    "model_override": "glm-5.3",
                    "labels": {"omnigent.access_lane": "glm-direct"},
                },
            )
        )
    )

    assert omniroute_cfg.access_lane == "omniroute"
    assert direct_cfg.access_lane == "glm-direct"
    assert omniroute_cfg.access_lane != direct_cfg.access_lane
