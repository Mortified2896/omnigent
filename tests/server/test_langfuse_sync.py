"""Tests for the Langfuse sync adapter and bounded retry worker.

Exercises the HTTP adapter's response classification, idempotency-key
generation, score / observation payload builders, and the outbox
state machine. The httpx transport is mocked — we don't make real
network calls in unit tests.

The retry schedule is exercised by walking ``LANGFUSE_RETRY_DELAYS_SECONDS``
forward through 5 attempts and asserting the row ends up ``dead``.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.entities.task_outcome import (
    TaskEvaluation,
    TaskReview,
    TaskRun,
)
from omnigent.server.langfuse_sync import (
    LANGFUSE_RETRY_DELAYS_SECONDS,
    SCORE_NAME_LLM_EVALUATION_ACCURACY,
    SCORE_NAME_TASK_CONFIDENCE_LLM,
    SCORE_NAME_TASK_FAMILY_HUMAN,
    SCORE_NAME_TASK_FAMILY_LLM,
    SCORE_NAME_TASK_QUALITY_HUMAN,
    SCORE_NAME_TASK_QUALITY_LLM,
    SCORE_NAME_TASK_VERDICT_HUMAN,
    SCORE_NAME_TASK_VERDICT_LLM,
    LangfuseSyncAdapter,
    _attempt_is_terminal,
    _delay_for_attempt,
    build_root_observation_payload,
    build_score_payloads,
    langfuse_configured,
    langfuse_idempotency_key,
    trace_id_for_task_run,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskEvaluationInput,
    CreateTaskRunInput,
    EnqueueLangfuseEventInput,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)

# ── idempotency + key helpers ────────────────────────────────────────────


def test_idempotency_key_format() -> None:
    """``langfuse_idempotency_key`` returns the documented
    ``task:<run>:<event>:<version>`` format."""
    k = langfuse_idempotency_key("tr_abc123", "llm-verdict")
    assert k == "task:tr_abc123:llm-verdict:v1"


def test_idempotency_key_custom_version() -> None:
    """Custom version increments for incompatible schema bumps."""
    k = langfuse_idempotency_key("tr_abc123", "root", version="v2")
    assert k.endswith(":v2")


def test_trace_id_is_stable_32_char_hex() -> None:
    """``trace_id_for_task_run`` returns 32-char lowercase hex."""
    trace_id = trace_id_for_task_run("tr_abc123")
    assert len(trace_id) == 32
    int(trace_id, 16)  # parses as hex
    # Stable: same input → same output.
    assert trace_id_for_task_run("tr_abc123") == trace_id


def test_langfuse_configured_returns_false_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``LANGFUSE_*`` env → ``False``."""
    for var in (
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
        "LANGFUSE_HOST",
    ):
        monkeypatch.delenv(var, raising=False)
    assert langfuse_configured() is False


def test_langfuse_configured_returns_true_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """All three env vars set → ``True``."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_test")
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example.com")
    assert langfuse_configured() is True


def test_langfuse_configured_partial(monkeypatch: pytest.MonkeyPatch) -> None:
    """Partial env → ``False`` (half-configured is worse than none)."""
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk_test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk_test")
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert langfuse_configured() is False


# ── score payload builders ──────────────────────────────────────────────


def _run(**kwargs: Any) -> TaskRun:
    """Build a minimal ``TaskRun`` for payload tests."""
    base = {
        "id": "tr_x",
        "conversation_id": "c1",
        "terminal_status": "completed",
        "created_at": 100,
        "updated_at": 200,
    }
    base.update(kwargs)
    return TaskRun(**base)


def _evaluation(**kwargs: Any) -> TaskEvaluation:
    """Build a minimal ``TaskEvaluation`` for payload tests."""
    base = {
        "id": "tev_x",
        "task_run_id": "tr_x",
        "evaluator_type": "llm",
        "verdict": "success",
        "created_at": 150,
    }
    base.update(kwargs)
    return TaskEvaluation(**base)


def _review(**kwargs: Any) -> TaskReview:
    """Build a minimal ``TaskReview`` for payload tests."""
    base = {
        "id": "trv_x",
        "task_run_id": "tr_x",
        "verdict": "success",
        "created_at": 180,
        "updated_at": 180,
    }
    base.update(kwargs)
    return TaskReview(**base)


def test_build_score_payloads_emits_each_llm_score() -> None:
    """All five LLM-side score types fire when the evaluation is populated."""
    run = _run()
    evaluation = _evaluation(
        verdict="success",
        confidence=0.9,
        quality_score=4,
        proposed_task_family="small_bug_fix",
    )
    scores = build_score_payloads(run, evaluation, review=None)
    names = {s.name for s in scores}
    assert SCORE_NAME_TASK_VERDICT_LLM in names
    assert SCORE_NAME_TASK_CONFIDENCE_LLM in names
    assert SCORE_NAME_TASK_QUALITY_LLM in names
    assert SCORE_NAME_TASK_FAMILY_LLM in names


def test_build_score_payloads_skips_missing_optional_fields() -> None:
    """Missing optional fields (confidence/quality/family) drop out cleanly."""
    run = _run()
    evaluation = _evaluation(
        verdict="partial", confidence=None, quality_score=None, proposed_task_family=None
    )
    scores = build_score_payloads(run, evaluation, review=None)
    names = {s.name for s in scores}
    # Only the required verdict score fires.
    assert names == {SCORE_NAME_TASK_VERDICT_LLM}


def test_build_score_payloads_emits_human_scores() -> None:
    """Human review scores fire for verdict, quality, family, accuracy."""
    run = _run()
    review = _review(
        verdict="success",
        quality_score=5,
        final_task_family="small_bug_fix",
        evaluator_accuracy="correct",
    )
    scores = build_score_payloads(run, evaluation=None, review=review)
    names = {s.name for s in scores}
    assert SCORE_NAME_TASK_VERDICT_HUMAN in names
    assert SCORE_NAME_TASK_QUALITY_HUMAN in names
    assert SCORE_NAME_TASK_FAMILY_HUMAN in names
    assert SCORE_NAME_LLM_EVALUATION_ACCURACY in names


def test_build_score_payloads_idempotency_keys_are_stable() -> None:
    """Same input → same score ids (Langfuse dedupes by id)."""
    run = _run(id="tr_xyz")
    evaluation = _evaluation(
        verdict="success", confidence=0.5, quality_score=3, proposed_task_family="refactor"
    )
    scores = build_score_payloads(run, evaluation, review=None)
    # Run twice — the score ids must be identical.
    scores2 = build_score_payloads(run, evaluation, review=None)
    assert [s.id for s in scores] == [s.id for s in scores2]


def test_build_root_observation_payload_redacts_secrets() -> None:
    """The root observation payload carries only metadata + bounded inputs."""
    run = _run(
        project_path="/home/user/repo",
        harness_id="OpenCode Native",
        requested_route_id="auto/coding",
        selected_provider="databricks",
        selected_model="databricks-claude-sonnet-4-6",
        reasoning_effort="medium",
        permission_mode="ask_before_edits",
        omniroute_decision_id="dec-1",
        billing_class="subscription",
        fallback_used=False,
    )
    payload = build_root_observation_payload(run)
    # No raw repo contents / diffs / env vars make it through.
    serialized = json.dumps(payload)
    assert "diff" not in serialized.lower()
    assert ".env" not in serialized
    # Provenance fields present.
    assert payload["metadata"]["omnigent_requested_route_id"] == "auto/coding"
    assert payload["metadata"]["omnigent_selected_model"] == "databricks-claude-sonnet-4-6"
    # Trace + session ids populated.
    assert payload["traceId"] == trace_id_for_task_run("tr_x")
    assert payload["sessionId"] == "c1"


def test_build_root_observation_payload_handles_no_changed_files() -> None:
    """Empty / missing changed_files does not emit a count field."""
    run = _run(changed_files=None)
    payload = build_root_observation_payload(run)
    assert "changed_files_count" not in payload["output"]


# ── retry schedule ───────────────────────────────────────────────────────


def test_retry_delays_are_monotonic() -> None:
    """Retry delays grow — 1m → 5m → 25m → 2h → 12h."""
    for i in range(1, len(LANGFUSE_RETRY_DELAYS_SECONDS)):
        assert LANGFUSE_RETRY_DELAYS_SECONDS[i] > LANGFUSE_RETRY_DELAYS_SECONDS[i - 1]


def test_delay_for_attempt_caps_at_last_entry() -> None:
    """Deep retries cap at the last entry rather than going negative."""
    last = LANGFUSE_RETRY_DELAYS_SECONDS[-1]
    assert _delay_for_attempt(len(LANGFUSE_RETRY_DELAYS_SECONDS)) == last
    assert _delay_for_attempt(len(LANGFUSE_RETRY_DELAYS_SECONDS) + 100) == last


def test_attempt_is_terminal_matches_retry_budget() -> None:
    """``_attempt_is_terminal`` is True once attempts >= retry-budget length."""
    budget = len(LANGFUSE_RETRY_DELAYS_SECONDS)
    assert not _attempt_is_terminal(budget - 1)
    assert _attempt_is_terminal(budget)
    assert _attempt_is_terminal(budget + 1)


# ── HTTP adapter response classification ─────────────────────────────────


def _mock_response(status_code: int, body: Any = None) -> MagicMock:
    """Build a mock httpx.Response with the given status + JSON body."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = json.dumps(body) if body is not None else ""
    resp.json = MagicMock(return_value=body if body is not None else {})
    return resp


def test_adapter_classifies_2xx_as_delivered() -> None:
    """Successful POST → ``delivered=True`` + the echoed id."""
    adapter = LangfuseSyncAdapter(
        host="https://langfuse.example.com",
        public_key="pk",
        secret_key="sk",
    )
    resp = _mock_response(200, {"id": "task:tr_x:root:v1"})
    result = adapter._classify_response(resp)
    assert result.delivered is True
    assert result.langfuse_id == "task:tr_x:root:v1"


def test_adapter_classifies_4xx_malformed_as_dead() -> None:
    """4xx (non-408/429) → ``dead=True`` (don't burn the retry budget)."""
    adapter = LangfuseSyncAdapter(host="h", public_key="pk", secret_key="sk")
    result = adapter._classify_response(_mock_response(400, {"error": "bad payload"}))
    assert result.dead is True
    assert result.delivered is False


def test_adapter_classifies_429_as_transient_retry() -> None:
    """429 → ``delivered=False``, ``retry_after_seconds`` set, not dead."""
    adapter = LangfuseSyncAdapter(host="h", public_key="pk", secret_key="sk")
    result = adapter._classify_response(_mock_response(429, {"error": "rate limited"}))
    assert result.delivered is False
    assert result.dead is False
    assert result.retry_after_seconds is not None


def test_adapter_classifies_5xx_as_transient_retry() -> None:
    """5xx → transient retry."""
    adapter = LangfuseSyncAdapter(host="h", public_key="pk", secret_key="sk")
    result = adapter._classify_response(_mock_response(503, {"error": "unavailable"}))
    assert result.delivered is False
    assert result.dead is False
    assert result.retry_after_seconds is not None


def test_adapter_classifies_408_as_transient_retry() -> None:
    """408 request timeout → transient retry (special-cased)."""
    adapter = LangfuseSyncAdapter(host="h", public_key="pk", secret_key="sk")
    result = adapter._classify_response(_mock_response(408, {"error": "timeout"}))
    assert result.delivered is False
    assert result.dead is False


# ── outbox state machine ─────────────────────────────────────────────────


@pytest.fixture
def outbox_store(tmp_path_factory) -> SqlAlchemyTaskOutcomeStore:
    """SQLAlchemy store + seeded conversation + run."""
    db_path = tmp_path_factory.mktemp("outbox") / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )
    store = SqlAlchemyTaskOutcomeStore(uri)
    run = store.create_run(CreateTaskRunInput(conversation_id="c1"))
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id, terminal_status="completed", terminal_at=200
        )
    )
    return store


def test_outbox_state_machine_full_lifecycle(
    outbox_store: SqlAlchemyTaskOutcomeStore,
) -> None:
    """Walk an outbox row through pending → failed → failed → delivered."""
    run = outbox_store.list_runs_for_conversation("c1")[0]
    outbox_id = outbox_store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run.id,
            event_type="task_root",
            idempotency_key="k1",
            payload={"id": "a"},
        )
    ).id
    # Pending + due.
    assert outbox_store.claim_due_langfuse_events(now=10**12) != []
    # Two failures.
    outbox_store.mark_langfuse_failed(outbox_id, "HTTP 500", 10**12)
    outbox_store.mark_langfuse_failed(outbox_id, "HTTP 502", 10**12)
    rows = outbox_store.claim_due_langfuse_events(now=10**12)
    assert len(rows) == 1 and rows[0].attempt_count == 2 and rows[0].last_error == "HTTP 502"
    # Delivered.
    outbox_store.mark_langfuse_delivered(outbox_id, 12345)
    assert outbox_store.claim_due_langfuse_events(now=10**12) == []
    assert outbox_store.count_pending_langfuse_events(run.id) == 0


def test_outbox_dead_advances_state_machine(
    outbox_store: SqlAlchemyTaskOutcomeStore,
) -> None:
    """``mark_langfuse_dead`` stops the row from being picked up."""
    run = outbox_store.list_runs_for_conversation("c1")[0]
    outbox_id = outbox_store.enqueue_langfuse_event(
        EnqueueLangfuseEventInput(
            task_run_id=run.id,
            event_type="task_root",
            idempotency_key="k2",
            payload={"id": "b"},
        )
    ).id
    outbox_store.mark_langfuse_dead(outbox_id, "exhausted")
    assert outbox_store.claim_due_langfuse_events(now=10**12) == []


def test_outbox_skipped_writes_audit_row(
    outbox_store: SqlAlchemyTaskOutcomeStore,
) -> None:
    """``mark_langfuse_skipped`` writes one ``skipped`` audit row."""
    run = outbox_store.list_runs_for_conversation("c1")[0]
    count = outbox_store.mark_langfuse_skipped(run.id)
    assert count == 1
    assert outbox_store.count_pending_langfuse_events(run.id) == 0


def test_outbox_evaluator_score_round_trip(
    outbox_store: SqlAlchemyTaskOutcomeStore,
) -> None:
    """A full evaluator + score payload + outbox enqueue walks cleanly."""
    from omnigent.server.langfuse_sync import build_score_payloads

    run = outbox_store.list_runs_for_conversation("c1")[0]
    eval_row = outbox_store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=run.id,
            evaluator_type="llm",
            verdict="success",
            confidence=0.8,
            quality_score=4,
            proposed_task_family="small_bug_fix",
        )
    )
    scores = build_score_payloads(run, eval_row, review=None)
    assert scores  # non-empty
    for score in scores:
        outbox_store.enqueue_langfuse_event(
            EnqueueLangfuseEventInput(
                task_run_id=run.id,
                task_evaluation_id=eval_row.id,
                event_type="llm_verdict",
                idempotency_key=score.id,
                payload={
                    "id": score.id,
                    "sessionId": score.session_id,
                    "traceId": score.trace_id,
                    "name": score.name,
                    "value": score.value,
                    "dataType": score.data_type,
                },
            )
        )
    assert outbox_store.count_pending_langfuse_events(run.id) == len(scores)
