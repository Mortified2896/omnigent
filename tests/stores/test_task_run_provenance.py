"""Persistence and idempotency contracts for structured runtime provenance."""

from __future__ import annotations

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.stores.task_outcome_store import (
    CreateRoutingDecisionInput,
    CreateRoutingProposalInput,
    CreateTaskRunInput,
    UpdateTaskRunProvenanceInput,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)


@pytest.fixture()
def store(tmp_path_factory) -> SqlAlchemyTaskOutcomeStore:
    db_path = tmp_path_factory.mktemp("provenance") / "provenance.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES "
                "('conv_1', 1, 1, 1, 'conv_1')"
            )
        )
    return SqlAlchemyTaskOutcomeStore(uri)


def _create_run(store: SqlAlchemyTaskOutcomeStore) -> str:

    proposal = store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id="conv_1",
            elicitation_id="elic_1",
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
            conversation_id="conv_1",
            response_id="resp_1",
            triggering_message_id="msg_1",
            routing_proposal_id=proposal.id,
            routing_decision_id=decision.id,
        )
    )
    return run.id


def test_update_run_provenance_persists_structured_metadata(
    store: SqlAlchemyTaskOutcomeStore,
) -> None:
    run_id = _create_run(store)
    updated = store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=True,
            fallback_used=True,
            omniroute_decision_id="dec_x",
            selection_strategy="round-robin",
            billing_class="subscription",
        )
    )
    assert updated is not None
    fetched = store.get_run(run_id)
    assert fetched is not None
    assert fetched.actual_provider == "codex"
    assert fetched.actual_provider_model == "codex/gpt-5.4-mini"
    assert fetched.actual_provenance_verified is True
    assert fetched.fallback_used is True
    assert fetched.omniroute_decision_id == "dec_x"
    assert fetched.selection_strategy == "round-robin"
    assert fetched.billing_class == "subscription"


def test_update_run_provenance_idempotent_on_duplicates(
    store: SqlAlchemyTaskOutcomeStore,
) -> None:
    run_id = _create_run(store)
    first = UpdateTaskRunProvenanceInput(
        task_run_id=run_id,
        actual_provider="codex",
        actual_provider_model="codex/gpt-5.4-mini",
        actual_provenance_verified=True,
    )
    store.update_run_provenance(first)
    store.update_run_provenance(first)
    fetched = store.get_run(run_id)
    assert fetched is not None
    assert fetched.actual_provider == "codex"
    assert fetched.actual_provider_model == "codex/gpt-5.4-mini"
    assert fetched.actual_provenance_verified is True


def test_update_run_provenance_does_not_erase_verified_identity(
    store: SqlAlchemyTaskOutcomeStore,
) -> None:
    run_id = _create_run(store)
    store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=True,
        )
    )
    store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider=None,
            actual_provider_model=None,
            actual_provenance_verified=False,
        )
    )
    fetched = store.get_run(run_id)
    assert fetched is not None
    assert fetched.actual_provider == "codex"
    assert fetched.actual_provider_model == "codex/gpt-5.4-mini"
    assert fetched.actual_provenance_verified is True


def test_update_run_provenance_unverified_when_provider_or_model_missing(
    store: SqlAlchemyTaskOutcomeStore,
) -> None:
    run_id = _create_run(store)
    store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="codex",
            actual_provider_model="codex/gpt-5.4-mini",
            actual_provenance_verified=False,
        )
    )
    fetched = store.get_run(run_id)
    assert fetched is not None
    assert fetched.actual_provider is None
    assert fetched.actual_provider_model is None
    assert fetched.actual_provenance_verified is False


def test_update_run_terminal_separates_proposed_from_actual(
    store: SqlAlchemyTaskOutcomeStore,
) -> None:
    run_id = _create_run(store)
    store.update_run_provenance(
        UpdateTaskRunProvenanceInput(
            task_run_id=run_id,
            actual_provider="minimax",
            actual_provider_model="minimax/MiniMax-M3",
            actual_provenance_verified=True,
            fallback_used=False,
        )
    )
    terminal = store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=1700000000,
        )
    )
    assert terminal is not None
    assert terminal.requested_route_id is None
    assert terminal.selected_provider is None
    assert terminal.actual_provider == "minimax"
    assert terminal.actual_provider_model == "minimax/MiniMax-M3"
    assert terminal.actual_provenance_verified is True
    assert terminal.fallback_used is False
