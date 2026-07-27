"""Recorder-level tests for structured runtime provenance."""

from __future__ import annotations

import pytest

from omnigent.server.task_outcome_recorder import (
    RoutingSnapshot,
    TaskOutcomeRecorder,
    stage_routing_snapshot,
)
from omnigent.stores.task_outcome_store import (
    CreateRoutingDecisionInput,
    CreateRoutingProposalInput,
    CreateTaskRunInput,
    TaskOutcomeStore,
    UpdateTaskRunProvenanceInput,
)


@pytest.fixture()
def recorder() -> TaskOutcomeRecorder:
    import tempfile
    from pathlib import Path

    from sqlalchemy import text

    from omnigent.db.utils import get_or_create_engine
    from omnigent.stores.task_outcome_store.sqlalchemy_store import (
        SqlAlchemyTaskOutcomeStore,
    )

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "recorder.db"
        engine = get_or_create_engine(f"sqlite:///{path}")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO conversations (id, created_at, updated_at, "
                    "kind, root_conversation_id) VALUES "
                    "('conv_rec', 1, 1, 1, 'conv_rec')"
                )
            )
        store = SqlAlchemyTaskOutcomeStore(f"sqlite:///{path}")
        yield TaskOutcomeRecorder(store=store)


def _seed_routed_run(store: TaskOutcomeStore, *, session_id: str = "conv_rec") -> str:
    proposal = store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id=session_id,
            elicitation_id="elic_rec",
            user_message="hello",
            content_types=["input_text"],
            original_route_id="auto/coding",
            requires_explicit_approval=True,
            proposal_payload={"intent": "code-review"},
        )
    )
    decision = store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=proposal.id,
            action="approved",
            decision_payload={"decision": "accept"},
            final_harness="opencode-native",
            final_route_id="auto/coding",
            final_reasoning_effort="high",
            final_permission_mode="default",
        )
    )
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id=session_id,
            response_id="resp_rec",
            triggering_message_id="msg_rec",
            routing_proposal_id=proposal.id,
            routing_decision_id=decision.id,
        )
    )
    return run.id


def test_provenance_recorder_stores_structured_identity(
    recorder: TaskOutcomeRecorder,
) -> None:
    run_id = _seed_routed_run(recorder.store)
    recorder.store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=True,
            fallback_used=True,
            omniroute_decision_id="dec-1",
        )
    )
    run = recorder.store.get_run(run_id)
    assert run is not None
    assert run.actual_provider == "codex"
    assert run.actual_provider_model == "codex/gpt-5.4-mini"
    assert run.actual_provenance_verified is True
    assert run.fallback_used is True
    assert run.omniroute_decision_id == "dec-1"


def test_provenance_recorder_refuses_unverified_metadata(
    recorder: TaskOutcomeRecorder,
) -> None:
    run_id = _seed_routed_run(recorder.store)
    recorder.store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=False,
        )
    )
    run = recorder.store.get_run(run_id)
    assert run is not None
    assert run.actual_provider is None
    assert run.actual_provider_model is None
    assert run.actual_provenance_verified is False


def test_provenance_recorder_later_null_does_not_erase_verified(
    recorder: TaskOutcomeRecorder,
) -> None:
    run_id = _seed_routed_run(recorder.store)
    recorder.store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=True,
        )
    )
    recorder.store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider=None,
            actual_provider_model=None,
            actual_provenance_verified=False,
        )
    )
    run = recorder.store.get_run(run_id)
    assert run is not None
    assert run.actual_provider == "codex"
    assert run.actual_provider_model == "codex/gpt-5.4-mini"
    assert run.actual_provenance_verified is True


def test_provenance_recorder_targets_active_routed_run(
    recorder: TaskOutcomeRecorder,
) -> None:
    run_id = _seed_routed_run(recorder.store)
    prior = recorder.store.get_run(run_id)
    assert prior is not None
    stage_routing_snapshot(
        "conv_rec",
        RoutingSnapshot(
            routing_proposal_id=prior.routing_proposal_id,
            routing_decision_id=prior.routing_decision_id,
        ),
    )
    from omnigent.entities import Conversation

    fake_conv = Conversation(
        id="conv_rec",
        created_at=1,
        updated_at=1,
        root_conversation_id="conv_rec",
        kind="default",
        title="rec",
        workspace=None,
        agent_id="agent_rec",
        harness_override="opencode-native",
    )
    recorder.on_response_in_progress(
        session_id="conv_rec",
        conversation=fake_conv,
        response_id="resp_rec_new",
        model_id=None,
        user_message_id="msg_rec_new",
        user_message_summary="hola",
        project_path=None,
    )
    recorder.on_response_provenance(
        session_id="conv_rec",
        actual_provider="codex",
        actual_provider_model="codex/gpt-5.4-mini",
        actual_provenance_verified=True,
    )
    runs = recorder.store.list_runs_for_conversation("conv_rec", limit=10)
    matches = [
        run
        for run in runs
        if run.actual_provider == "codex" and run.actual_provider_model == "codex/gpt-5.4-mini"
    ]
    assert matches, "provenance should attach to the active routed run"
    for run in matches:
        assert run.actual_provenance_verified is True
