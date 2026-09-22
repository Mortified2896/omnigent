"""Authenticated API and isolation coverage for the model advisor surface.

Uses the real AdvisorRepository on a disposable migrated database, a stub
host registry that answers ``host.advisor_call`` frames synchronously, and a
stub session launcher. No provider calls and no live host are involved.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import Any

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.model_advisor_repository import AdvisorRepository
from omnigent.server.auth import AuthProvider
from omnigent.server.model_advisor_service import ModelAdvisorService
from omnigent.server.routes.model_advisor import create_model_advisor_router


class _HeaderAuth(AuthProvider):
    """Deterministic identity from a test header."""

    def get_user_id(self, request: Any) -> str | None:
        return request.headers.get("x-test-user")


class _FakeHost:
    def __init__(self, host_id: str, user_id: str | None) -> None:
        self.host_id = host_id
        self.user_id = user_id


class _FakeHostStore:
    def __init__(self, hosts: list[_FakeHost]) -> None:
        self._hosts = {host.host_id: host for host in hosts}

    def get_host(self, host_id: str) -> _FakeHost | None:
        return self._hosts.get(host_id)


class _FakeConnection:
    def __init__(self, reply: dict[str, Any]) -> None:
        self.host_id = "host_1"
        self.pending_advisor_calls: dict[str, asyncio.Future] = {}
        self.pending_model_options: dict[str, asyncio.Future] = {}
        self.reply = reply


class _FakeRegistry:
    def __init__(self, conn: _FakeConnection) -> None:
        self._conn = conn
        self.sent: list[Any] = []
        self.advisor_calls = 0

    def get(self, _host_id: str) -> _FakeConnection | None:
        return self._conn

    def send_text(self, conn: _FakeConnection, raw: Any) -> None:
        from omnigent.host.frames import (
            HostAdvisorCallFrame,
            HostModelOptionsFrame,
            decode_host_frame,
        )

        self.sent.append(raw)
        frame = decode_host_frame(raw) if isinstance(raw, str) else raw
        if isinstance(frame, HostModelOptionsFrame):
            future = conn.pending_model_options.pop(frame.request_id, None)
            result = {"status": "ok", "models": [dict(row) for row in CATALOG_MODELS]}
        elif isinstance(frame, HostAdvisorCallFrame):
            self.advisor_calls += 1
            future = conn.pending_advisor_calls.pop(frame.request_id, None)
            result = conn.reply
        else:  # pragma: no cover - test guard
            raise AssertionError(f"unexpected frame {frame!r}")
        if future is not None and not future.done():
            future.set_result(result)


class _FakeConversationStore:
    def get_conversation(self, session_id: str) -> Any:
        return None


class _FakeSession:
    id = "conv_new"


async def _async_session(session: _FakeSession) -> _FakeSession:
    return session


CATALOG_MODELS = [
    {
        "id": "gpt-5.3-codex",
        "model": "gpt-5.3-codex",
        "displayName": "GPT-5.3 Codex",
        "accessLane": "codex-direct",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
        "isDefault": True,
    },
    {
        "id": "glm-5.3",
        "model": "glm-5.3",
        "displayName": "GLM-5.3",
        "accessLane": "glm-direct",
        "defaultReasoningEffort": "high",
        "supportedReasoningEfforts": [{"reasoningEffort": "high"}],
    },
    # A generic gateway row with an OpenAI-looking name: never qualified.
    {"id": "gpt-5.6", "model": "gpt-5.6", "accessLane": "omniroute", "isDefault": True},
]


def _candidate_id(lane: str, model: str, effort: str) -> str:
    """Compute the stable candidate id exactly as the service does."""
    identity = (
        lane,
        "openai-codex-subscription" if lane == "codex-direct" else "z.ai",
        "codex-login" if lane == "codex-direct" else "zai-direct",
        "codex",
        model,
        effort,
        "chatgpt_plan" if lane == "codex-direct" else "glm_plan",
    )
    wire = json.dumps(identity, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "choice-" + hashlib.sha256(wire.encode()).hexdigest()


HUMAN_ID = _candidate_id("codex-direct", "gpt-5.3-codex", "medium")
ADVISOR_ID = _candidate_id("glm-direct", "glm-5.3", "high")

PREFERENCES_BODY = {
    "host_id": "host_1",
    "expected_version": 0,
    "preferences": {
        "enabled": True,
        "allowed_candidate_ids": [HUMAN_ID],
        "advisor_candidate_id": ADVISOR_ID,
        "human_probability_percent": 50,
    },
}

ALICE = {"X-Test-User": "alice@test"}
JSON_ALICE = {**ALICE, "Content-Type": "application/json"}

ADVISOR_OK_REPLY = {
    "status": "ok",
    "raw_output": json.dumps({"candidate_id": HUMAN_ID, "rationale": "Balanced pick."}),
    "latency_ms": 1200,
    "input_tokens": 100,
    "output_tokens": 20,
    "cached_input_tokens": 0,
    "response_id": None,
}


def client_for(
    db_uri: str,
    *,
    auth: AuthProvider | None = _HeaderAuth(),
    reply: dict[str, Any] | None = None,
    hosts: list[_FakeHost] | None = None,
    launcher: Any = None,
    randbelow: Any = None,
) -> tuple[TestClient, _FakeRegistry]:
    repository = AdvisorRepository(get_or_create_engine(db_uri))
    conn = _FakeConnection(reply if reply is not None else dict(ADVISOR_OK_REPLY))
    registry = _FakeRegistry(conn)
    service_kwargs: dict[str, Any] = {}
    if randbelow is not None:
        service_kwargs["randbelow"] = randbelow
    service = ModelAdvisorService(
        repository=repository,
        host_store=_FakeHostStore(hosts or [_FakeHost("host_1", "alice@test")]),
        host_registry=registry,
        conversation_store=_FakeConversationStore(),
        session_launcher=launcher
        or (lambda body, *, user_id, request=None: _async_session(_FakeSession())),
        **service_kwargs,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _errors(request: Request, exc: OmnigentError) -> JSONResponse:
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(
        create_model_advisor_router(auth_provider=auth, service=service), prefix="/v1"
    )
    return TestClient(app), registry


def _settle(client: TestClient, round_id: str, *, want_overhead: bool = True) -> dict[str, Any]:
    """Poll a round past its bounded advisor call (stub reply is immediate)."""
    payload: dict[str, Any] = {}
    for _ in range(300):
        payload = client.get(
            f"/v1/model-advisor/rounds/{round_id}?host_id=host_1", headers=ALICE
        ).json()
        settled = payload["state"] != "advisor_pending"
        if settled and (not want_overhead or "advisor_overhead" in payload):
            return payload
        time_sleep()
    raise AssertionError("round never settled")


def time_sleep() -> None:
    import time as _time

    _time.sleep(0.01)


def test_preferences_roundtrip_and_owner_isolation(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        created = client.put(
            "/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE
        )
        assert created.status_code == 200
        saved = created.json()
        assert saved["version"] == 1
        assert saved["preferences"]["enabled"] is True

        loaded = client.get("/v1/model-advisor/preferences?host_id=host_1", headers=ALICE).json()
        assert loaded["version"] == 1
        assert loaded["preferences"]["advisor_candidate_id"] == ADVISOR_ID

        # Another (valid) identity that does not own the host is forbidden
        # outright — records are scoped to owner+host, and host ownership is
        # checked before any record access.
        bob = client.get(
            "/v1/model-advisor/preferences?host_id=host_1", headers={"X-Test-User": "bob@test"}
        )
        assert bob.status_code == 403


def test_preferences_cas_conflict(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        first = client.put(
            "/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE
        )
        assert first.status_code == 200
        stale = client.put(
            "/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE
        )
        assert stale.status_code == 409


def test_saved_candidate_ids_must_be_qualified_catalog_entries(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        body = {
            **PREFERENCES_BODY,
            "preferences": {
                **PREFERENCES_BODY["preferences"],
                "allowed_candidate_ids": ["choice-unknown"],
            },
        }
        rejected = client.put("/v1/model-advisor/preferences", json=body, headers=JSON_ALICE)
        assert rejected.status_code == 422


def test_host_ownership_enforced(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        client.put("/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE)
        # Bob does not own host_1: every surface scoped to it is forbidden,
        # even though he presents a valid identity.
        bob = {"X-Test-User": "bob@test"}
        assert (
            client.get("/v1/model-advisor/preferences?host_id=host_1", headers=bob).status_code
            == 403
        )
        assert (
            client.get("/v1/model-advisor/catalog?host_id=host_1", headers=bob).status_code == 403
        )
        attacker = client.put(
            "/v1/model-advisor/preferences",
            json=PREFERENCES_BODY,
            headers={**bob, "Content-Type": "application/json"},
        )
        assert attacker.status_code == 403
        # And Alice's saved row is untouched by the rejected attempts.
        assert (
            client.get("/v1/model-advisor/preferences?host_id=host_1", headers=ALICE).json()[
                "version"
            ]
            == 1
        )
        unknown = client.get("/v1/model-advisor/preferences?host_id=missing", headers=ALICE)
        assert unknown.status_code == 404


def test_round_lifecycle_with_confirmation(db_uri) -> None:
    launched: list[Any] = []

    async def launcher(body: Any, *, user_id: str | None, request: Any = None) -> Any:
        launched.append((body, user_id))
        return _FakeSession()

    client, registry = client_for(db_uri, launcher=launcher)
    with client:
        assert (
            client.put(
                "/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE
            ).status_code
            == 200
        )
        create = client.post(
            "/v1/model-advisor/rounds",
            json={
                "host_id": "host_1",
                "task": "Write the acceptance suite",
                "human_candidate_id": HUMAN_ID,
            },
            headers=JSON_ALICE,
        )
        assert create.status_code == 200
        payload = create.json()
        assert payload["state"] in {"advisor_pending", "awaiting_confirmation"}
        round_id = payload["round_id"]

        # The frozen snapshot (full task + pool) must never reach the browser.
        assert "frozen" not in create.text
        assert "Write the acceptance suite" not in create.text

        payload = _settle(client, round_id)
        assert payload["state"] == "awaiting_confirmation"
        # The stub advice picks the human candidate, so this round agrees.
        assert payload["review"]["human_candidate_id"] == HUMAN_ID
        assert payload["review"]["advisor_candidate_id"] == HUMAN_ID
        assert payload["review"]["assigned_arm"] == "same"
        assert payload["review"]["comparison_group"] == "agreement"
        assert payload["advisor_overhead"]["latency_ms"] == 1200

        # Exactly one bounded advisor call frame reached the host.
        assert registry.advisor_calls == 1

        confirm = client.post(
            f"/v1/model-advisor/rounds/{round_id}/confirm",
            json={
                "host_id": "host_1",
                "expected_version": payload["version"],
                "launch": {"agent_id": "ag_1", "workspace": "/repo"},
            },
            headers=JSON_ALICE,
        )
        assert confirm.status_code == 200
        confirmed = confirm.json()
        assert confirmed["state"] == "dispatch_bound"
        assert confirmed["execution"]["session_id"] == "conv_new"
        assert len(launched) == 1
        body = launched[0][0]
        assert body.model_override == "gpt-5.3-codex"
        assert body.reasoning_effort == "medium"
        assert body.labels["omnigent.access_lane"] == "codex-direct"

        # A second confirm cannot launch anything else.
        again = client.post(
            f"/v1/model-advisor/rounds/{round_id}/confirm",
            json={
                "host_id": "host_1",
                "expected_version": confirmed["version"],
                "launch": {"agent_id": "ag_1", "workspace": "/repo"},
            },
            headers=JSON_ALICE,
        )
        assert again.status_code == 200
        assert len(launched) == 1


def test_round_freezes_explicit_draft_and_reuses_submission_key(db_uri) -> None:
    """Unsaved round settings are authoritative, and retries do not call twice."""
    reply = {
        **ADVISOR_OK_REPLY,
        "raw_output": json.dumps({"candidate_id": ADVISOR_ID, "rationale": "GLM fits."}),
    }
    client, registry = client_for(db_uri, reply=reply)
    draft = {
        "enabled": True,
        "allowed_candidate_ids": [ADVISOR_ID],
        "advisor_candidate_id": HUMAN_ID,
        "human_probability_percent": 20,
    }
    body = {
        "host_id": "host_1",
        "task": "Use the unsaved draft",
        "human_candidate_id": ADVISOR_ID,
        "submission_key": "stable-double-click",
        "preferences": draft,
    }
    with client:
        client.put("/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE)
        first = client.post("/v1/model-advisor/rounds", json=body, headers=JSON_ALICE)
        second = client.post("/v1/model-advisor/rounds", json=body, headers=JSON_ALICE)
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["round_id"] == first.json()["round_id"]
        settled = _settle(client, first.json()["round_id"])
        assert settled["review"]["human_candidate_id"] == ADVISOR_ID
        assert settled["review"]["advisor_candidate_id"] == ADVISOR_ID
        assert settled["review"]["human_probability_percent"] == 20
        assert registry.advisor_calls == 1


def test_override_preserves_original_and_launches_choice(db_uri) -> None:
    launched: list[Any] = []

    def launcher(body: Any, *, user_id: str | None, request: Any = None) -> Any:
        launched.append(body)
        return _FakeSession()

    # Advice disagrees (picks the GLM candidate); the deterministic draw
    # (bit 1) assigns the advisor's pick for a meaningful override scenario.
    reply = {
        "status": "ok",
        "raw_output": json.dumps({"candidate_id": ADVISOR_ID, "rationale": "GLM fits."}),
        "latency_ms": 900,
    }
    client, _registry = client_for(
        db_uri, reply=reply, launcher=launcher, randbelow=lambda n: n - 1
    )
    with client:
        body = {
            **PREFERENCES_BODY,
            "preferences": {
                **PREFERENCES_BODY["preferences"],
                "allowed_candidate_ids": [HUMAN_ID, ADVISOR_ID],
            },
        }
        client.put("/v1/model-advisor/preferences", json=body, headers=JSON_ALICE)
        create = client.post(
            "/v1/model-advisor/rounds",
            json={
                "host_id": "host_1",
                "task": "task",
                "human_candidate_id": HUMAN_ID,
            },
            headers=JSON_ALICE,
        )
        round_id = create.json()["round_id"]
        payload = _settle(client, round_id)
        assert payload["review"]["assigned_candidate_id"] == ADVISOR_ID
        overridden = client.post(
            f"/v1/model-advisor/rounds/{round_id}/confirm",
            json={
                "host_id": "host_1",
                "expected_version": payload["version"],
                "override_candidate_id": HUMAN_ID,
                "reason": "Codex is better for this repo",
                "launch": {"agent_id": "ag_1", "workspace": "/repo"},
            },
            headers=JSON_ALICE,
        )
        assert overridden.status_code == 200
        review = overridden.json()["review"]
        assert review["overridden"] is True
        assert review["comparison_group"] == "manual_override"
        # The original assignment is retained, not rewritten.
        assert review["assigned_candidate_id"] == ADVISOR_ID
        assert review["assigned_arm"] == "advisor"
        assert launched[0].model_override == "gpt-5.3-codex"


def test_advisor_failure_blocks_the_round(db_uri) -> None:
    human_id = HUMAN_ID
    reply = {"status": "failed", "raw_output": None, "error": "advisor transport failed"}
    client, _registry = client_for(db_uri, reply=reply)
    with client:
        client.put("/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE)
        created = client.post(
            "/v1/model-advisor/rounds",
            json={"host_id": "host_1", "task": "task", "human_candidate_id": human_id},
            headers=JSON_ALICE,
        )
        round_id = created.json()["round_id"]
        payload = _settle(client, round_id, want_overhead=False)
        assert payload["state"] == "blocked"
        assert "advisor transport failed" in payload["failure_reason"]


def test_invalid_advisor_output_blocks_the_round(db_uri) -> None:
    reply = {"status": "ok", "raw_output": "definitely not json", "latency_ms": 5}
    client, _registry = client_for(db_uri, reply=reply)
    with client:
        client.put("/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE)
        created = client.post(
            "/v1/model-advisor/rounds",
            json={"host_id": "host_1", "task": "task", "human_candidate_id": HUMAN_ID},
            headers=JSON_ALICE,
        )
        payload = _settle(client, created.json()["round_id"], want_overhead=False)
        assert payload["state"] == "blocked"
        assert "advisor output rejected" in payload["failure_reason"]


def test_cancel_before_confirmation(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        client.put("/v1/model-advisor/preferences", json=PREFERENCES_BODY, headers=JSON_ALICE)
        created = client.post(
            "/v1/model-advisor/rounds",
            json={"host_id": "host_1", "task": "task", "human_candidate_id": HUMAN_ID},
            headers=JSON_ALICE,
        )
        round_id = created.json()["round_id"]
        payload = _settle(client, round_id, want_overhead=False)
        cancelled = client.post(
            f"/v1/model-advisor/rounds/{round_id}/cancel",
            json={"host_id": "host_1", "expected_version": payload["version"]},
            headers=JSON_ALICE,
        )
        assert cancelled.status_code == 200
        assert cancelled.json()["state"] == "cancelled"
        stale_cancel = client.post(
            f"/v1/model-advisor/rounds/{round_id}/cancel",
            json={"host_id": "host_1", "expected_version": payload["version"]},
            headers=JSON_ALICE,
        )
        assert stale_cancel.status_code == 409


def test_unauthenticated_requests_are_rejected(db_uri) -> None:
    client, _registry = client_for(db_uri)
    with client:
        assert client.get("/v1/model-advisor/catalog?host_id=host_1").status_code == 401
        assert client.get("/v1/model-advisor/preferences?host_id=host_1").status_code == 401


def test_disabled_advisor_blocks_round_creation(db_uri) -> None:
    client, _registry = client_for(db_uri)
    disabled = {
        "host_id": "host_1",
        "expected_version": 0,
        "preferences": {
            "enabled": False,
            "allowed_candidate_ids": [],
            "advisor_candidate_id": None,
            "human_probability_percent": 50,
        },
    }
    with client:
        client.put("/v1/model-advisor/preferences", json=disabled, headers=JSON_ALICE)
        rejected = client.post(
            "/v1/model-advisor/rounds",
            json={"host_id": "host_1", "task": "task", "human_candidate_id": "whatever"},
            headers=JSON_ALICE,
        )
        assert rejected.status_code == 422
        assert "disabled" in rejected.json()["detail"]


@pytest.mark.parametrize("missing", ["advisor_candidate_id", "allowed_candidate_ids"])
def test_incomplete_enabled_preferences_are_rejected(db_uri, missing: str) -> None:
    client, _registry = client_for(db_uri)
    preferences = {
        "enabled": True,
        "allowed_candidate_ids": [HUMAN_ID],
        "advisor_candidate_id": ADVISOR_ID,
        "human_probability_percent": 50,
    }
    preferences[missing] = None if missing == "advisor_candidate_id" else []
    with client:
        rejected = client.put(
            "/v1/model-advisor/preferences",
            json={"host_id": "host_1", "expected_version": 0, "preferences": preferences},
            headers=JSON_ALICE,
        )
        assert rejected.status_code == 422


def test_advisor_label_namespace_is_server_only() -> None:
    """Clients cannot seed omnigent.advisor.* at session creation."""
    import pytest as _pytest

    from omnigent.errors import OmnigentError
    from omnigent.server.routes._sessions.helpers import _reject_server_reserved_label_seed

    _reject_server_reserved_label_seed({"team": "ml"})
    with _pytest.raises(OmnigentError, match="server-internal"):
        _reject_server_reserved_label_seed({"omnigent.advisor.round_id": "forged"})


def test_internal_launch_skips_client_label_guard(monkeypatch) -> None:
    """The advisor's server-side launch seeds omnigent.advisor.* itself.

    The shared creation path must keep validating the seed on the public
    route, but the internal caller opts out — otherwise the advisor's own
    confirm launch is refused by the guard meant for clients.
    """
    import asyncio
    import contextlib

    import omnigent.server.routes._sessions.orchestration as orchestration
    from omnigent.server.schemas import SessionCreateRequest

    seen: list[dict[str, str] | None] = []
    real_guard = orchestration._reject_server_reserved_label_seed

    def spy(labels):
        seen.append(labels)
        return real_guard(labels)

    monkeypatch.setattr(orchestration, "_reject_server_reserved_label_seed", spy)
    body = SessionCreateRequest(
        agent_id="no-such-agent",
        labels={"omnigent.advisor.round_id": "adviseround-regression"},
    )

    async def attempt(enforce: bool) -> None:
        # Later store-less failures are fine; this seam pins the guard.
        with contextlib.suppress(Exception):
            await orchestration._create_session_from_existing_agent(
                None,
                None,
                None,
                body,
                None,
                enforce_reserved_label_seed=enforce,
            )

    asyncio.run(attempt(True))
    assert seen == [body.labels]
    asyncio.run(attempt(False))
    assert seen == [body.labels]


def test_internal_launch_with_advisor_labels_reaches_real_creation_helper(monkeypatch) -> None:
    """The trusted internal flag reaches the shared creator and persists labels."""
    import asyncio
    from types import SimpleNamespace

    import omnigent.server.routes._session_create_validation as validation
    import omnigent.server.routes._sessions.orchestration as orchestration
    from omnigent.entities import Agent, Conversation
    from omnigent.server.schemas import SessionCreateRequest

    created: list[dict[str, Any]] = []
    labels = {"omnigent.advisor.round_id": "adviseround-real-path"}
    agent = Agent(
        id="ag_advisor",
        created_at=1,
        name="advisor-test",
        bundle_location="bundle-advisor",
    )

    class Store:
        def create_conversation(self, **kwargs: Any) -> Conversation:
            created.append(kwargs)
            return Conversation(
                id="conv_real_path",
                created_at=1,
                updated_at=1,
                root_conversation_id="conv_real_path",
                agent_id=kwargs["agent_id"],
                title=kwargs["title"],
                host_id=kwargs["host_id"],
                workspace=kwargs["workspace"],
                terminal_launch_args=kwargs["terminal_launch_args"],
            )

        def set_labels(self, _session_id: str, new_labels: dict[str, str]) -> None:
            assert new_labels == labels

    async def resolve(**kwargs: Any) -> Any:
        return SimpleNamespace(body=kwargs["body"], project_id=None, warnings=())

    async def snapshot(*_args: Any, **_kwargs: Any) -> Any:
        return SimpleNamespace(id="conv_real_path")  # type: ignore[return-value]

    async def reject_routing(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def attempt() -> tuple[Any, Store]:
        async def validate(**_kwargs: Any) -> Agent:
            return agent

        monkeypatch.setattr(orchestration, "validate_session_agent", validate)
        monkeypatch.setattr(validation, "resolve_project_session_create", resolve)
        monkeypatch.setattr(orchestration, "_reject_ungatewayed_model_routing", reject_routing)
        monkeypatch.setattr(orchestration, "_get_session_snapshot", snapshot)
        body = SessionCreateRequest(agent_id=agent.id, labels=labels)
        store = Store()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(host_registry=None)),
            headers={},
        )
        result = await orchestration._create_session_from_existing_agent(
            store,
            None,
            None,
            body,
            request,
            user_id="alice@test",
            enforce_reserved_label_seed=False,
        )
        return result, store

    result, _store = asyncio.run(attempt())
    assert result[0].id == "conv_real_path"
    assert created[0]["agent_id"] == agent.id
