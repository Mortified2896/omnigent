"""New chats enter the creator's review queue; accepting them stays durable."""

from __future__ import annotations

import json
import time
from typing import Any

import httpx
import pytest

from omnigent.server.auth import LEVEL_READ
from omnigent.stores.conversation_store import PINNED_LABEL_KEY, pinned_label_key
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import build_agent_bundle, create_test_agent

pytestmark = pytest.mark.asyncio


async def create_chat(
    client: httpx.AsyncClient,
    form: str,
    *,
    labels: dict[str, str] | None = None,
    user: str | None = None,
    parent_session_id: str | None = None,
) -> httpx.Response:
    headers = {"X-Forwarded-Email": user} if user else {}
    metadata: dict[str, Any] = {"title": "Review this outcome", "labels": labels or {}}
    if parent_session_id:
        metadata["parent_session_id"] = parent_session_id
    if form == "json":
        agent = await create_test_agent(client, user=user)
        metadata["agent_id"] = agent["id"]
        return await client.post("/v1/sessions", json=metadata, headers=headers)
    return await client.post(
        "/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("agent.tar.gz", build_agent_bundle("review-agent"), "application/gzip")},
        headers=headers,
    )


def created_id(response: httpx.Response) -> str:
    assert response.status_code == 201, response.text
    body = response.json()
    return body.get("id") or body["session_id"]


@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_new_chat_starts_pinned_and_unpin_survives_updates(
    client: httpx.AsyncClient, db_uri: str, form: str
) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    existing = store.create_conversation(title="Previously accepted")
    started = time.time_ns() // 1_000_000
    sid = created_id(await create_chat(client, form, labels={"review-topic": "release"}))
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    pin = snapshot["labels"][PINNED_LABEL_KEY]
    assert started <= int(pin) <= time.time_ns() // 1_000_000
    assert snapshot["labels"]["review-topic"] == "release"
    stored = store.get_conversation(sid)
    assert stored is not None
    assert stored.labels[pinned_label_key(None)] == pin
    assert PINNED_LABEL_KEY not in stored.labels
    pinned = (await client.get("/v1/sessions?pinned=true")).json()["data"]
    assert sid in {row["id"] for row in pinned}
    assert existing.id not in {row["id"] for row in pinned}

    accepted = await client.patch(f"/v1/sessions/{sid}", json={"labels": {PINNED_LABEL_KEY: ""}})
    assert accepted.status_code == 200, accepted.text
    updated = await client.patch(f"/v1/sessions/{sid}", json={"title": "Accepted outcome"})
    assert updated.status_code == 200, updated.text
    reloaded = (await client.get(f"/v1/sessions/{sid}")).json()
    assert PINNED_LABEL_KEY not in reloaded["labels"]
    assert not reloaded["archived"]
    pinned = (await client.get("/v1/sessions?pinned=true")).json()["data"]
    assert sid not in {row["id"] for row in pinned}
    assert store.get_conversation(existing.id).labels == {}


@pytest.mark.parametrize("form", ["json", "multipart"])
@pytest.mark.parametrize("pin", ["", "1721760000000"])
async def test_explicit_create_pin_uses_caller_key(
    client: httpx.AsyncClient, form: str, pin: str
) -> None:
    sid = created_id(await create_chat(client, form, labels={PINNED_LABEL_KEY: pin}))
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    assert snapshot["labels"].get(PINNED_LABEL_KEY) == (pin or None)


@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_child_sessions_do_not_enter_review_queue(
    client: httpx.AsyncClient, form: str
) -> None:
    parent = created_id(await create_chat(client, "multipart"))
    child = created_id(await create_chat(client, form, parent_session_id=parent))
    snapshot = (await client.get(f"/v1/sessions/{child}")).json()
    assert PINNED_LABEL_KEY not in snapshot["labels"]


@pytest.mark.parametrize("side_chat", [False, True])
async def test_fork_gets_fresh_pin_without_copying_anyone_elses(
    client: httpx.AsyncClient, db_uri: str, side_chat: bool
) -> None:
    source = created_id(await create_chat(client, "multipart", labels={PINNED_LABEL_KEY: "1"}))
    store = SqlAlchemyConversationStore(db_uri)
    store.set_labels(source, {pinned_label_key("someone-else"): "2"})
    started = time.time_ns() // 1_000_000
    fork = await client.post(f"/v1/sessions/{source}/fork", json={"side_chat": side_chat})
    sid = created_id(fork)
    snapshot = (await client.get(f"/v1/sessions/{sid}")).json()
    stored = store.get_conversation(sid)
    assert stored is not None
    assert pinned_label_key("someone-else") not in stored.labels
    if side_chat:
        assert PINNED_LABEL_KEY not in snapshot["labels"]
    else:
        assert int(snapshot["labels"][PINNED_LABEL_KEY]) >= started
    assert store.get_conversation(source).labels[pinned_label_key(None)] == "1"


@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_new_chat_is_pinned_only_for_its_creator(
    auth_client: httpx.AsyncClient, db_uri: str, form: str
) -> None:
    alice, bob = "alice@example.com", "bob@example.com"
    created = await create_chat(auth_client, form, user=alice)
    sid = created_id(created)
    if form == "json":
        assert PINNED_LABEL_KEY in created.json()["labels"]
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.ensure_user(bob)
    permissions.grant(bob, sid, LEVEL_READ)
    alice_snapshot = await auth_client.get(
        f"/v1/sessions/{sid}", headers={"X-Forwarded-Email": alice}
    )
    bob_snapshot = await auth_client.get(f"/v1/sessions/{sid}", headers={"X-Forwarded-Email": bob})
    assert alice_snapshot.status_code == bob_snapshot.status_code == 200
    assert PINNED_LABEL_KEY in alice_snapshot.json()["labels"]
    assert PINNED_LABEL_KEY not in bob_snapshot.json()["labels"]
    assert not any(key.startswith(f"{PINNED_LABEL_KEY}.") for key in bob_snapshot.json()["labels"])
    stored = SqlAlchemyConversationStore(db_uri).get_conversation(sid)
    assert stored is not None
    assert pinned_label_key(alice) in stored.labels
    assert pinned_label_key(None) not in stored.labels

    fork = await auth_client.post(
        f"/v1/sessions/{sid}/fork", json={}, headers={"X-Forwarded-Email": bob}
    )
    fork_id = created_id(fork)
    assert PINNED_LABEL_KEY in fork.json()["labels"]
    fork_row = SqlAlchemyConversationStore(db_uri).get_conversation(fork_id)
    assert fork_row is not None
    assert pinned_label_key(bob) in fork_row.labels
    assert pinned_label_key(alice) not in fork_row.labels


@pytest.mark.parametrize("form", ["json", "multipart"])
async def test_create_cannot_pin_for_another_user(client: httpx.AsyncClient, form: str) -> None:
    response = await create_chat(client, form, labels={pinned_label_key("someone-else"): "1"})
    assert response.status_code == 400, response.text
