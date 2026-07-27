"""Tests for the LLM task-outcome evaluator.

The evaluator is the only piece of the vertical slice that calls
the model. Tests run against the SQLAlchemy store + a mocked
``PolicyLLMClient`` so we can pin every output variant
(success/partial/failure/inconclusive) and every failure mode
(timeout, schema violation, invalid JSON).

These tests don't talk to OmniRoute or any provider — the
``PolicyLLMClient.create`` is patched at the module boundary.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.entities.task_outcome import TaskRun
from omnigent.server.task_outcome_evaluator import (
    EVALUATOR_JSON_SCHEMA,
    EvaluatorEvidence,
    build_evaluator_prompt,
    collect_evidence,
    evaluate_task_outcome,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskRunInput,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)


@pytest.fixture
def store(tmp_path_factory) -> TaskOutcomeStore:
    """SQLAlchemy store + seeded conversation + terminalised run."""
    db_path = tmp_path_factory.mktemp("eval") / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )
    s = SqlAlchemyTaskOutcomeStore(uri)
    run = s.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="r1",
            task_description="Fix the login bug.",
            requested_route_id="auto/coding",
            selected_provider="databricks",
            selected_model="databricks-claude-sonnet-4-6",
            reasoning_effort="medium",
            permission_mode="ask_before_edits",
            omniroute_decision_id="dec-1",
            billing_class="subscription",
            fallback_used=False,
        )
    )
    s.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=200,
            input_tokens=1500,
            output_tokens=300,
            total_cost_usd=0.05,
            response_summary="Fixed the auth flow.",
            changed_files=["src/auth.py"],
            commit_sha="abc1234567",
        )
    )
    s.request_evaluation(run.id, "minimax/MiniMax-M3")
    return s


def _run(store: TaskOutcomeStore) -> TaskRun:
    runs = store.list_runs_for_conversation("c1")
    assert runs, "store fixture should have seeded a run"
    return runs[0]


def _ok_response(payload: dict[str, Any]) -> MagicMock:
    """Build a successful ``PolicyLLMClient.create`` response.

    The persisted ``custom/outcome-scoring`` combo is M3-only by
    construction. The wire id surface is the combo name while the
    actual model identity stays ``minimax/MiniMax-M3``.
    """
    resp = MagicMock()
    resp.output = [MagicMock(content=[MagicMock(text=json.dumps(payload))])]
    resp.provider_metadata = {
        "x-omniroute-requested-model": "custom/outcome-scoring",
        "x-omniroute-selected-provider": "minimax",
        "x-omniroute-selected-model": "minimax/MiniMax-M3",
        "x-omniroute-fallback-used": "false",
        "x-omniroute-decision-id": "decision-test",
    }
    return resp


def _make_client(return_value_or_exc: Any) -> MagicMock:
    """Build a fake :class:`PolicyLLMClient` with realistic string fields.

    The ``_model``/``_connection``/``_request_timeout`` slots are
    real string values (not MagicMock sentinels) so they pass
    through to SQL parameter binding without TypeErrors.
    """
    client = MagicMock()
    client._model = "omniroute/custom/outcome-scoring"
    client._connection = None
    client._request_timeout = 60
    if isinstance(return_value_or_exc, BaseException):
        client.create = AsyncMock(side_effect=return_value_or_exc)
    else:
        client.create = AsyncMock(return_value=return_value_or_exc)
    return client


# ── prompt builder ───────────────────────────────────────────────────────


def test_evidence_round_trip() -> None:
    """``collect_evidence`` reports None for unavailable fields — never passed."""
    run = TaskRun(
        id="tr_x",
        conversation_id="c1",
        terminal_status="completed",
        created_at=100,
        updated_at=200,
        duration_ms=10000,
        changed_files=["src/a.py", "src/b.py"],
    )
    evidence = collect_evidence(run)
    assert evidence.terminal_status == "completed"
    assert evidence.changed_files_count == 2
    assert evidence.commit_sha is None
    assert evidence.tests_passed is None
    assert evidence.input_tokens is None


def test_prompt_includes_all_sections() -> None:
    """The prompt surfaces task + routing + evidence + response + verdict vocabulary."""
    run = TaskRun(
        id="tr_x",
        conversation_id="c1",
        terminal_status="completed",
        created_at=100,
        updated_at=200,
        task_description="Fix the login bug.",
        requested_route_id="auto/coding",
        selected_model="databricks-claude-sonnet-4-6",
        response_summary="Fixed it.",
    )
    prompt = build_evaluator_prompt(
        run,
        triggering_message_summary="Fix the login bug",
        evidence=EvaluatorEvidence(terminal_status="completed"),
    )
    assert "success" in prompt
    assert "inconclusive" in prompt
    assert "small_bug_fix" in prompt
    assert "routing provenance" in prompt.lower()
    assert "available objective evidence" in prompt.lower()
    assert "final agent response" in prompt.lower()


def test_prompt_truncates_long_inputs() -> None:
    """The prompt's bounded inputs stay under the cap even with extreme sizes."""
    run = TaskRun(
        id="tr_x",
        conversation_id="c1",
        terminal_status="completed",
        created_at=100,
        updated_at=200,
        task_description="X" * 100_000,
        response_summary="Y" * 100_000,
    )
    prompt = build_evaluator_prompt(
        run,
        triggering_message_summary="Z" * 100_000,
        evidence=EvaluatorEvidence(terminal_status="completed"),
    )
    # The prompt is bounded — even with extreme inputs the
    # total is below the documented cap.
    assert len(prompt) <= 24_001


# ── evaluator schema ────────────────────────────────────────────────────


def test_evaluator_schema_admits_required_fields() -> None:
    """Schema lists every required field."""
    required = set(EVALUATOR_JSON_SCHEMA["required"])
    expected = {
        "verdict",
        "confidence",
        "task_family",
        "reasoning",
        "evidence",
        "unresolved_issues",
    }
    assert expected <= required


def test_evaluator_schema_verdict_enum_matches_vocabulary() -> None:
    """The schema's verdict enum is the documented vocabulary."""
    from omnigent.entities.task_outcome import TASK_VERDICTS

    enum = EVALUATOR_JSON_SCHEMA["properties"]["verdict"]["enum"]
    assert set(enum) == set(TASK_VERDICTS)


def test_evaluator_schema_family_enum_matches_vocabulary() -> None:
    """The schema's task_family enum is the documented vocabulary."""
    from omnigent.entities.task_outcome import TASK_FAMILIES

    enum = EVALUATOR_JSON_SCHEMA["properties"]["task_family"]["enum"]
    assert set(enum) == set(TASK_FAMILIES)


# ── happy path: success verdict → success row ────────────────────────────


@pytest.mark.asyncio
async def test_evaluator_records_success(store: TaskOutcomeStore) -> None:
    """A well-formed success response lands as ``verdict='success'``."""
    run = _run(store)
    ok_payload = {
        "verdict": "success",
        "confidence": 0.9,
        "quality": 5,
        "task_family": "small_bug_fix",
        "reasoning": "Tests pass.",
        "evidence": ["unit tests green"],
        "unresolved_issues": [],
    }
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(_ok_response(ok_payload)), None),
    ):
        outcome = await evaluate_task_outcome(
            store, run, triggering_message_summary="Fix login bug"
        )
    assert outcome.evaluation.verdict == "success"
    assert outcome.evaluation.confidence == 0.9
    assert outcome.evaluation.quality_score == 5
    assert outcome.evaluation.proposed_task_family == "small_bug_fix"
    assert outcome.evaluation.evidence == ["unit tests green"]
    assert "task:tr" in outcome.langfuse_evaluation_id


@pytest.mark.asyncio
async def test_evaluator_records_partial(store: TaskOutcomeStore) -> None:
    """A ``partial`` verdict + reasonable fields land as a partial row."""
    run = _run(store)
    payload = {
        "verdict": "partial",
        "confidence": 0.6,
        "quality": 3,
        "task_family": "feature_implementation",
        "reasoning": "Implemented but a test still flaky.",
        "evidence": ["new tests added"],
        "unresolved_issues": ["flaky integration test"],
    }
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(_ok_response(payload)), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.evaluation.verdict == "partial"
    assert outcome.evaluation.confidence == 0.6


@pytest.mark.asyncio
async def test_evaluator_records_failure(store: TaskOutcomeStore) -> None:
    """A ``failure`` verdict (low confidence) is captured faithfully."""
    run = _run(store)
    payload = {
        "verdict": "failure",
        "confidence": 0.95,
        "quality": 1,
        "task_family": "test_failure_repair",
        "reasoning": "Tests still failing.",
        "evidence": ["CI red"],
        "unresolved_issues": ["permission denied on test"],
    }
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(_ok_response(payload)), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.evaluation.verdict == "failure"


# ── failure modes ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluator_failed_when_no_llm_configured(
    store: TaskOutcomeStore,
) -> None:
    """Missing server configuration is visible and creates no judgment row."""
    run = _run(store)
    from omnigent.server.task_outcome_evaluator import EvaluatorFailure

    failure = EvaluatorFailure(
        "configuration", "missing_evaluator_config", "RuntimeCaps.llm is None", False
    )
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(None, failure),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.status == "failed"
    assert outcome.evaluation is None
    assert store.get_evaluation_for_run(run.id) is None
    persisted = store.get_run(run.id)
    assert persisted is not None
    assert persisted.evaluation_status == "failed"
    assert persisted.evaluation_error_code == "missing_evaluator_config"


@pytest.mark.asyncio
async def test_evaluator_deferred_on_llm_timeout(
    store: TaskOutcomeStore,
) -> None:
    """A timeout defers M3 and creates no judgment row."""
    run = _run(store)
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(TimeoutError("upstream timeout")), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.status == "deferred"
    assert outcome.evaluation is None
    assert store.get_evaluation_for_run(run.id) is None
    persisted = store.get_run(run.id)
    assert persisted is not None
    assert persisted.evaluation_status == "deferred"
    assert persisted.evaluation_error_code == "timeout"


@pytest.mark.asyncio
async def test_evaluator_inconclusive_on_invalid_json(
    store: TaskOutcomeStore,
) -> None:
    """A non-JSON LLM response → inconclusive row."""
    run = _run(store)
    bad = MagicMock()
    bad.output = [MagicMock(content=[MagicMock(text="not json at all")])]
    bad.provider_metadata = _ok_response({}).provider_metadata
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(bad), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.status == "failed"
    assert outcome.evaluation is None
    assert store.get_evaluation_for_run(run.id) is None
    persisted = store.get_run(run.id)
    assert persisted is not None
    assert persisted.evaluation_error_code == "malformed_structured_output"


@pytest.mark.asyncio
async def test_evaluator_inconclusive_on_schema_violation(
    store: TaskOutcomeStore,
) -> None:
    """A JSON response that violates the schema → inconclusive row."""
    run = _run(store)
    bad_payload = {
        "verdict": "garbage_value",
        "confidence": 0.5,
        "task_family": "refactor",
        "reasoning": "test",
        "evidence": [],
        "unresolved_issues": [],
    }
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(_ok_response(bad_payload)), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.status == "failed"
    assert outcome.evaluation is None
    assert store.get_evaluation_for_run(run.id) is None
    persisted = store.get_run(run.id)
    assert persisted is not None
    assert persisted.evaluation_error_code == "malformed_structured_output"


@pytest.mark.asyncio
async def test_evaluator_inconclusive_on_missing_text(
    store: TaskOutcomeStore,
) -> None:
    """A response with empty content → inconclusive."""
    run = _run(store)
    bad = MagicMock()
    bad.output = [MagicMock(content=[MagicMock(text="")])]
    bad.provider_metadata = _ok_response({}).provider_metadata
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(bad), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.status == "failed"
    assert outcome.evaluation is None
    assert store.get_evaluation_for_run(run.id) is None
    persisted = store.get_run(run.id)
    assert persisted is not None
    assert persisted.evaluation_error_code == "malformed_structured_output"


# ── shape invariants ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_evaluator_records_provenance_when_present(
    store: TaskOutcomeStore,
) -> None:
    """The evaluator stamps provider / model / route_id from the LLM client onto the row."""
    run = _run(store)
    payload = {
        "verdict": "success",
        "confidence": 0.9,
        "quality": 4,
        "task_family": "refactor",
        "reasoning": "ok",
        "evidence": [],
        "unresolved_issues": [],
    }
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(_make_client(_ok_response(payload)), None),
    ):
        outcome = await evaluate_task_outcome(store, run)
    assert outcome.evaluation.evaluator_provider == "minimax"
    assert outcome.evaluation.evaluator_model == "minimax/MiniMax-M3"
    assert outcome.evaluation.evaluator_fallback_used is False
