"""End-to-end route proof for the structured runtime provenance event.

Exercises the full chain introduced for routed OpenCode-native executions:

  runner callback POST ``external_execution_provenance``
  → ``POST /v1/sessions/{id}/events``
  → validation in ``routes/sessions``
  → :class:`TaskOutcomeRecorder.on_response_provenance`
  → :class:`SqlAlchemyTaskOutcomeStore.update_run_provenance`

The test seeds a routed run by invoking the relay's
``on_response_in_progress`` against the active routed snapshot so the
recorder has a row to attach provenance to.
"""

from __future__ import annotations

import pytest

from omnigent.entities import Conversation
from omnigent.server.task_outcome_recorder import (
    RoutingSnapshot,
    TaskOutcomeRecorder,
    stage_routing_snapshot,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.task_outcome_store import (
    CreateRoutingDecisionInput,
    CreateRoutingProposalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _seed_routed_run(client, db_uri: str, recorder: TaskOutcomeRecorder) -> tuple[str, str]:
    agent = await create_test_agent(client)
    session = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert session.status_code == 201, session.text
    session_id = session.json()["id"]
    proposal = recorder.store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id=session_id,
            elicitation_id=f"elic_{session_id}",
            user_message="hello",
            content_types=["input_text"],
            original_route_id="auto/coding:reliable",
            requires_explicit_approval=True,
            proposal_payload={"intent": "code-review"},
        )
    )
    decision = recorder.store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=proposal.id,
            action="approved",
            decision_payload={"decision": "accept"},
            final_harness="opencode-native",
            final_route_id="auto/coding:reliable",
            final_reasoning_effort="high",
            final_permission_mode="default",
        )
    )
    stage_routing_snapshot(
        session_id,
        RoutingSnapshot(
            routing_proposal_id=proposal.id,
            routing_decision_id=decision.id,
        ),
    )
    snapshot = SqlAlchemyConversationStore(db_uri).get_conversation(session_id)
    assert snapshot is not None
    run_id = recorder.on_response_in_progress(
        session_id=session_id,
        conversation=Conversation(
            id=session_id,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            root_conversation_id=session_id,
            kind=snapshot.kind,
            title=snapshot.title,
            workspace=snapshot.workspace,
            agent_id=snapshot.agent_id,
            harness_override="opencode-native",
        ),
        response_id=f"resp_{session_id}",
        model_id=None,
        user_message_id=f"msg_{session_id}",
        user_message_summary="hello",
        project_path=None,
    )
    assert run_id is not None
    return session_id, run_id


@pytest.fixture()
def provenance_recorder(db_uri: str):
    """Install a recorder backed by the test's shared DB.

    The integration ``client`` fixture wires the running ASGI app
    without a task-outcome store, so we install our own recorder
    before each test and reset it on teardown.
    """
    from omnigent.server.task_outcome_recorder import set_recorder

    recorder = TaskOutcomeRecorder(store=SqlAlchemyTaskOutcomeStore(db_uri))
    set_recorder(recorder)
    try:
        yield recorder
    finally:
        set_recorder(None)


async def test_external_execution_provenance_event_persists_actual_identity(
    client, db_uri: str, provenance_recorder: TaskOutcomeRecorder
) -> None:
    """The route validates and forwards structured metadata to the recorder."""
    session_id, run_id = await _seed_routed_run(client, db_uri, provenance_recorder)

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_execution_provenance",
            "data": {
                "actual_provider": "codex",
                "actual_provider_model": "codex/gpt-5.4-mini",
                "actual_provenance_verified": True,
                "fallback_used": False,
                "omniroute_decision_id": "dec-runtime-1",
                "selection_strategy": "round-robin",
                "billing_class": "subscription",
            },
        },
    )
    assert resp.status_code == 202, resp.text
    assert resp.json() == {"queued": False}

    run = provenance_recorder.store.get_run(run_id)
    assert run is not None
    assert run.actual_provider == "codex"
    assert run.actual_provider_model == "codex/gpt-5.4-mini"
    assert run.actual_provenance_verified is True
    assert run.fallback_used is False
    assert run.omniroute_decision_id == "dec-runtime-1"
    assert run.selection_strategy == "round-robin"
    assert run.billing_class == "subscription"


async def test_external_execution_provenance_rejects_unverified_payload(client) -> None:
    """A verified flag without provider/model must fail input validation."""
    agent = await create_test_agent(client)
    session = await client.post(
        "/v1/sessions",
        json={"agent_id": agent["id"]},
    )
    assert session.status_code == 201
    session_id = session.json()["id"]

    resp = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_execution_provenance",
            "data": {
                "actual_provenance_verified": True,
                "actual_provider": "",
                "actual_provider_model": " ",
            },
        },
    )
    assert resp.status_code >= 400, resp.text


async def test_external_execution_provenance_with_unverified_metadata_does_not_erase(
    client, db_uri: str, provenance_recorder: TaskOutcomeRecorder
) -> None:
    """Late unverified callbacks cannot overwrite a previously verified run."""
    session_id, run_id = await _seed_routed_run(client, db_uri, provenance_recorder)

    first = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_execution_provenance",
            "data": {
                "actual_provider": "codex",
                "actual_provider_model": "codex/gpt-5.4-mini",
                "actual_provenance_verified": True,
            },
        },
    )
    assert first.status_code == 202, first.text

    # A second callback claiming verified but with empty provider/model must
    # fail input validation rather than erase the verified identity.
    second = await client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "external_execution_provenance",
            "data": {
                "actual_provider": "",
                "actual_provider_model": "",
                "actual_provenance_verified": True,
            },
        },
    )
    assert second.status_code >= 400, second.text

    run = provenance_recorder.store.get_run(run_id)
    assert run is not None
    assert run.actual_provider == "codex"
    assert run.actual_provider_model == "codex/gpt-5.4-mini"
    assert run.actual_provenance_verified is True
