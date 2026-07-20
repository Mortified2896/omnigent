"""Tests for the task-outcome recorder + routing-snapshot staging.

Exercises the recorder's relay-facing API (``on_response_in_progress``
+ ``on_response_terminal``) and the snapshot staging used by
``_await_route_approval``. The recorder's LLM-evaluator spawn
is mocked so we can pin its dispatch without spinning up an
event loop.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.server.task_outcome_recorder import (
    RoutingSnapshot,
    TaskOutcomeRecorder,
    consume_routing_snapshot,
    discard_routing_snapshot,
    get_recorder,
    peek_routing_snapshot,
    set_recorder,
    stage_routing_snapshot,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskEvaluationInput,
    CreateTaskRunInput,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)


@pytest.fixture
def store(tmp_path_factory) -> TaskOutcomeStore:
    """SQLAlchemy store + seeded conversation."""
    db_path = tmp_path_factory.mktemp("rec") / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )
    return SqlAlchemyTaskOutcomeStore(uri)


def _conversation() -> Any:
    """Build a minimal :class:`Conversation`-like duck type."""
    conv = MagicMock()
    conv.workspace = "/home/user/repo"
    conv.harness_override = None
    conv.agent_id = "ag_abc"
    conv.omniroute_route_id = None
    return conv


# ── snapshot staging ────────────────────────────────────────────────────


def test_stage_then_consume_clears_snapshot() -> None:
    """``stage`` writes; ``consume`` returns + clears."""
    snapshot = RoutingSnapshot(
        requested_route_id="auto/coding",
        reasoning_effort="medium",
        permission_mode="ask_before_edits",
        omniroute_decision_id="dec-1",
        billing_class="subscription",
        fallback_used=False,
    )
    stage_routing_snapshot("c1", snapshot)
    assert peek_routing_snapshot("c1") is not None
    out = consume_routing_snapshot("c1")
    assert out is snapshot
    assert peek_routing_snapshot("c1") is None


def test_consume_returns_none_when_unset() -> None:
    """Consuming an un-staged session returns ``None``."""
    assert consume_routing_snapshot("nonexistent-session") is None


def test_discard_routing_snapshot_prevents_manual_turn_inheritance() -> None:
    """Turning routing off must invalidate an accepted proposal."""
    stage_routing_snapshot("c1", RoutingSnapshot(requested_route_id="auto/coding"))
    discard_routing_snapshot("c1")
    assert consume_routing_snapshot("c1") is None


def test_stage_overwrites_existing() -> None:
    """Re-staging for the same session overwrites (latest wins)."""
    snap_a = RoutingSnapshot(requested_route_id="auto/coding")
    snap_b = RoutingSnapshot(requested_route_id="auto/reasoning")
    stage_routing_snapshot("c1", snap_a)
    stage_routing_snapshot("c1", snap_b)
    assert consume_routing_snapshot("c1") is snap_b


# ── recorder: on_response_in_progress ──────────────────────────────────


def test_recorder_creates_run_on_in_progress(
    store: TaskOutcomeStore,
) -> None:
    """``on_response_in_progress`` creates a ``TaskRun`` row."""
    recorder = TaskOutcomeRecorder(store=store)
    stage_routing_snapshot(
        "c1",
        RoutingSnapshot(
            requested_route_id="auto/coding",
            reasoning_effort="medium",
            permission_mode="ask_before_edits",
            omniroute_decision_id="dec-1",
            billing_class="subscription",
            fallback_used=False,
        ),
    )
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r1",
        model_id="databricks/databricks-claude-sonnet-4-6",
        user_message_id=None,
        user_message_summary="Fix login",
        project_path="/home/user/repo",
    )
    assert run_id is not None
    run = store.get_run(run_id)
    assert run is not None
    assert run.terminal_status == "running"
    assert run.response_id == "r1"
    assert run.task_description == "Fix login"
    assert run.requested_route_id == "auto/coding"
    assert run.selected_model == "databricks/databricks-claude-sonnet-4-6"
    # An approved combo is requested through OmniRoute even when a harness
    # happens to report a concrete-looking model label.
    assert run.selected_provider == "omniroute"


def test_recorder_creates_run_when_no_snapshot(
    store: TaskOutcomeStore,
) -> None:
    """Without a staged snapshot the row still creates; routing cols stay NULL."""
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r2",
        model_id="openai/gpt-5-mini",
        user_message_id=None,
        user_message_summary="Help me",
        project_path=None,
    )
    run = store.get_run(run_id)
    assert run is not None
    assert run.requested_route_id is None
    assert run.selected_model == "openai/gpt-5-mini"
    assert run.selected_provider == "openai"


def test_recorder_creates_manual_omniroute_run_from_session_selection(
    store: TaskOutcomeStore,
) -> None:
    recorder = TaskOutcomeRecorder(store=store)
    conversation = _conversation()
    conversation.omniroute_route_id = "auto/coding:reliable"

    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=conversation,
        response_id="r-direct-route",
        model_id="auto/coding:reliable",
        user_message_id="msg-1",
        user_message_summary="Inspect status",
        project_path=None,
    )

    run = store.get_run(run_id)
    assert run is not None
    assert run.routing_proposal_id is None
    assert run.routing_decision_id is None
    assert run.requested_route_id == "auto/coding:reliable"
    assert run.selected_provider == "omniroute"
    assert run.selected_model == "auto/coding:reliable"


def test_recorder_handles_in_progress_failure_gracefully(
    store: TaskOutcomeStore,
) -> None:
    """A store-side failure in ``on_response_in_progress`` is logged, not raised."""
    broken_store = MagicMock()
    broken_store.create_run = MagicMock(side_effect=RuntimeError("db gone"))
    broken_recorder = TaskOutcomeRecorder(store=broken_store)
    # No exception escapes.
    run_id = broken_recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r3",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )
    assert run_id is None


# ── recorder: on_response_terminal ──────────────────────────────────────


def test_recorder_terminalises_run_on_completed(
    store: TaskOutcomeStore,
) -> None:
    """``on_response_terminal`` advances the row + enqueues the evaluator."""
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r1",
        model_id="databricks/databricks-claude-sonnet-4-6",
        user_message_id=None,
        user_message_summary="Fix login",
        project_path=None,
    )
    # Mock the evaluator spawn so the test doesn't need an event loop.
    with (
        patch.object(recorder, "_spawn_evaluator") as spawn_evaluator,
        patch.object(recorder, "_enqueue_langfuse_for_run") as enqueue_langfuse,
    ):
        recorder.on_response_terminal(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=200,
            input_tokens=1500,
            output_tokens=300,
            total_cost_usd=0.05,
            response_summary="Fixed it.",
            changed_files=["src/auth.py"],
            commit_sha="abc1234567",
        )
    run = store.get_run(run_id)
    assert run is not None
    assert run.terminal_status == "completed"
    assert run.terminal_at == 200
    assert run.duration_ms is not None
    assert run.input_tokens == 1500
    assert run.changed_files == ["src/auth.py"]
    assert run.commit_sha == "abc1234567"
    # Both side-effects ran.
    assert spawn_evaluator.call_count == 1
    assert enqueue_langfuse.call_count == 1


def test_recorder_captures_failure_error_code(
    store: TaskOutcomeStore,
) -> None:
    """``on_response_terminal`` captures ``failure_error_code`` for failed runs."""
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r1",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )
    with (
        patch.object(recorder, "_spawn_evaluator"),
        patch.object(recorder, "_enqueue_langfuse_for_run"),
    ):
        recorder.on_response_terminal(
            task_run_id=run_id,
            terminal_status="failed",
            terminal_at=200,
            failure_error_code="tool_error",
            failure_error_message="npm install failed: ENOENT",
        )
    run = store.get_run(run_id)
    assert run is not None
    assert run.terminal_status == "failed"
    assert run.failure_error_code == "tool_error"
    assert run.failure_error_message == "npm install failed: ENOENT"


def test_recorder_terminal_unknown_run_is_noop(
    store: TaskOutcomeStore,
) -> None:
    """An unknown task_run_id returns None and does NOT raise."""
    recorder = TaskOutcomeRecorder(store=store)
    with (
        patch.object(recorder, "_spawn_evaluator") as spawn_evaluator,
        patch.object(recorder, "_enqueue_langfuse_for_run") as enqueue_langfuse,
    ):
        recorder.on_response_terminal(
            task_run_id="nonexistent",
            terminal_status="completed",
            terminal_at=200,
        )
    # No evaluator / Langfuse side-effects fire on a missing row.
    assert spawn_evaluator.call_count == 0
    assert enqueue_langfuse.call_count == 0


def test_spawn_evaluator_persists_inconclusive_when_no_loop(
    store: TaskOutcomeStore,
) -> None:
    """A worker without the lifespan loop still gets a durable evaluation."""
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r-no-loop",
        model_id=None,
        user_message_id=None,
        user_message_summary="Say hi",
        project_path=None,
    )

    recorder.on_response_terminal(
        task_run_id=run_id,
        terminal_status="completed",
        terminal_at=200,
        response_summary="Hi",
    )

    evaluation = store.get_evaluation_for_run(run_id)
    assert evaluation is not None
    assert evaluation.verdict == "inconclusive"
    assert "no event loop available" in (evaluation.reasoning or "")


def test_spawn_evaluator_idempotent_when_evaluation_exists(
    store: TaskOutcomeStore,
) -> None:
    """Terminal dispatch does not append a second evaluation row."""
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r-existing-evaluation",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )
    existing = store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=run_id,
            evaluator_type="llm",
            verdict="inconclusive",
            reasoning="Already evaluated.",
        )
    )

    recorder.on_response_terminal(
        task_run_id=run_id,
        terminal_status="completed",
        terminal_at=200,
    )

    persisted = store.get_evaluation_for_run(run_id)
    assert persisted is not None
    assert persisted.id == existing.id


def test_spawn_evaluator_uses_main_loop_when_available(
    store: TaskOutcomeStore,
) -> None:
    """The worker dispatches through the captured FastAPI event loop."""
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    loop.is_running.return_value = True
    loop.is_closed.return_value = False
    spawner_calls: list[tuple[Any, asyncio.AbstractEventLoop]] = []

    def _spawner(coroutine: Any) -> Any:
        spawner_calls.append((coroutine, loop))
        coroutine.close()
        return MagicMock()

    recorder = TaskOutcomeRecorder(store=store, _loop=loop, _task_spawner=_spawner)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r-main-loop",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )

    recorder.on_response_terminal(
        task_run_id=run_id,
        terminal_status="completed",
        terminal_at=200,
    )

    assert len(spawner_calls) == 1
    coroutine, scheduled_loop = spawner_calls[0]
    assert scheduled_loop is loop
    assert asyncio.iscoroutine(coroutine)
    coroutine.close()  # mirroring the spawner's duty; safe to call again
    assert store.get_evaluation_for_run(run_id) is None


def test_re_evaluate_terminal_run_creates_evaluation(
    store: TaskOutcomeStore,
) -> None:
    """Manual repair uses the same durable no-loop fallback."""
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="r-manual-evaluate",
            task_description="Repair the missing outcome.",
        )
    )
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=200,
        )
    )
    recorder = TaskOutcomeRecorder(store=store)

    status = recorder.re_evaluate(run.id)

    assert status == "failed_persisted"
    evaluation = store.get_evaluation_for_run(run.id)
    assert evaluation is not None
    assert evaluation.verdict == "inconclusive"


# ── recorder: langfuse sync dispatch ────────────────────────────────────


def test_recorder_skipped_audit_row_when_langfuse_unset(
    store: TaskOutcomeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Langfuse env is unset, terminalise writes a ``skipped`` audit row."""
    for var in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r1",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )
    with patch.object(recorder, "_spawn_evaluator"):
        recorder.on_response_terminal(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=200,
        )
    # The skipped audit row was written; pending count is 0.
    assert store.count_pending_langfuse_events(run_id) == 0


def test_recorder_enqueues_root_observation_when_langfuse_set(
    store: TaskOutcomeStore,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When Langfuse env is fully wired, terminalise enqueues a real ``task_root`` outbox row."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.com")
    recorder = TaskOutcomeRecorder(store=store)
    run_id = recorder.on_response_in_progress(
        session_id="c1",
        conversation=_conversation(),
        response_id="r1",
        model_id=None,
        user_message_id=None,
        user_message_summary=None,
        project_path=None,
    )
    with patch.object(recorder, "_spawn_evaluator"):
        recorder.on_response_terminal(
            task_run_id=run_id,
            terminal_status="completed",
            terminal_at=200,
        )
    # One pending outbox row exists.
    assert store.count_pending_langfuse_events(run_id) == 1


# ── global recorder registry ────────────────────────────────────────────


def test_global_recorder_getter_returns_set_value(
    store: TaskOutcomeStore,
) -> None:
    """``set_recorder`` installs a recorder; ``get_recorder`` returns it."""
    recorder = TaskOutcomeRecorder(store=store)
    set_recorder(recorder)
    try:
        assert get_recorder() is recorder
    finally:
        set_recorder(None)


def test_global_recorder_clear_returns_none() -> None:
    """``set_recorder(None)`` clears the registry."""
    set_recorder(None)
    assert get_recorder() is None
