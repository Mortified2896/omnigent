"""Focused tests for durable fixed-M3 evaluator lifecycle semantics."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from sqlalchemy import text

from omnigent.db.utils import get_or_create_engine
from omnigent.server.task_outcome_evaluator import (
    FIXED_EVALUATOR_MODEL,
    classify_evaluator_error,
    evaluate_task_outcome,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskRunInput,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import SqlAlchemyTaskOutcomeStore


@pytest.fixture
def pending_store(tmp_path) -> TaskOutcomeStore:
    uri = f"sqlite:///{tmp_path / 'm3.db'}"
    engine = get_or_create_engine(uri)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, kind, "
                "root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )
    store = SqlAlchemyTaskOutcomeStore(uri)
    run = store.create_run(CreateTaskRunInput(conversation_id="c1", response_id="r1"))
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=(run.started_at or 0) + 1,
        )
    )
    store.request_evaluation(run.id, FIXED_EVALUATOR_MODEL)
    return store


def _response(
    *,
    requested: str = "custom/outcome-scoring",
    model: str = FIXED_EVALUATOR_MODEL,
    fallback: str = "false",
) -> MagicMock:
    payload = {
        "verdict": "inconclusive",
        "confidence": 0.4,
        "quality": None,
        "task_family": "other",
        "reasoning": "M3 found insufficient objective evidence.",
        "evidence": [],
        "unresolved_issues": ["No test result was available."],
    }
    response = MagicMock()
    response.output = [MagicMock(content=[MagicMock(text=json.dumps(payload))])]
    provider = model.split("/", 1)[0] if "/" in model else "unknown"
    response.provider_metadata = {
        "x-omniroute-requested-model": requested,
        "x-omniroute-selected-provider": provider,
        "x-omniroute-selected-model": model,
        "x-omniroute-fallback-used": fallback,
    }
    return response


@pytest.mark.parametrize(
    ("status", "expected_transient"),
    [
        (429, True),
        (408, True),
        (502, True),
        (503, True),
        (504, True),
        (401, False),
        (403, False),
        (400, False),
        (404, False),
    ],
)
def test_http_classification(status: int, expected_transient: bool) -> None:
    response = httpx.Response(status, request=httpx.Request("POST", "http://gateway"))
    error = httpx.HTTPStatusError("failure", request=response.request, response=response)
    classified = classify_evaluator_error(error)
    assert classified.transient is expected_transient


@pytest.mark.parametrize(
    "message",
    ["provider cooldown", "ALL_ACCOUNTS_INACTIVE", "plan quota exhausted"],
)
def test_gateway_availability_markers_are_transient(message: str) -> None:
    assert classify_evaluator_error(RuntimeError(message)).transient is True


@pytest.mark.parametrize("exc", [TimeoutError("slow"), ConnectionError("refused")])
def test_transport_failures_are_transient(exc: BaseException) -> None:
    assert classify_evaluator_error(exc).transient is True


def test_configuration_model_is_pinned(monkeypatch: pytest.MonkeyPatch) -> None:
    from omnigent.runtime import _globals
    from omnigent.runtime.caps import RuntimeCaps
    from omnigent.server.task_outcome_evaluator import _configured_policy_client
    from omnigent.spec.types import LLMConfig

    original = _globals._caps
    try:
        monkeypatch.setattr(
            _globals,
            "_caps",
            RuntimeCaps(
                llm=LLMConfig(
                    model="omniroute/custom/outcome-scoring",
                    connection={
                        "base_url": "http://127.0.0.1:20128/v1",
                        "api_key": "test",
                    },
                )
            ),
        )
        client, failure = _configured_policy_client()
        assert failure is None
        assert client is not None
        assert client._model == "omniroute/custom/outcome-scoring"
    finally:
        monkeypatch.setattr(_globals, "_caps", original)


@pytest.mark.asyncio
async def test_valid_m3_inconclusive_is_completed(pending_store: TaskOutcomeStore) -> None:
    run = pending_store.list_runs_for_conversation("c1")[0]
    client = MagicMock(create=AsyncMock(return_value=_response()))
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(client, None),
    ):
        outcome = await evaluate_task_outcome(pending_store, run)
    assert outcome.status == "completed"
    assert outcome.evaluation is not None
    assert outcome.evaluation.verdict == "inconclusive"
    assert outcome.evaluation.reasoning == "M3 found insufficient objective evidence."
    assert pending_store.get_run(run.id).evaluation_status == "completed"


@pytest.mark.asyncio
async def test_restart_worker_recovers_due_deferred(pending_store: TaskOutcomeStore) -> None:
    from omnigent.server.task_outcome_recorder import TaskOutcomeRecorder
    from omnigent.server.task_outcome_retry import run_evaluation_retry_worker

    run = pending_store.list_runs_for_conversation("c1")[0]
    pending_store.mark_evaluation_deferred(
        run.id,
        error_kind="availability",
        error_code="503",
        error_message="temporarily unavailable",
        next_retry_at=1,
    )
    recorder = TaskOutcomeRecorder(store=pending_store)
    stop = __import__("asyncio").Event()
    stop.set()
    with patch.object(recorder, "dispatch_claimed_evaluation", return_value="queued") as dispatch:
        await run_evaluation_retry_worker(recorder, stop_event=stop)
    dispatch.assert_called_once()
    assert pending_store.get_run(run.id).evaluation_status == "pending"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("requested", "model", "fallback"),
    [
        ("custom/outcome-scoring", "openai/gpt-5.4", "false"),
        ("custom/outcome-scoring", FIXED_EVALUATOR_MODEL, "true"),
        # Bare minimax/MiniMax-M3 wire id is no longer accepted as a
        # requested-model surface; the combo name is the canonical surface.
        (FIXED_EVALUATOR_MODEL, FIXED_EVALUATOR_MODEL, "false"),
    ],
)
async def test_provenance_violation_creates_no_evaluation(
    pending_store: TaskOutcomeStore, requested: str, model: str, fallback: str
) -> None:
    run = pending_store.list_runs_for_conversation("c1")[0]
    client = MagicMock(
        create=AsyncMock(
            return_value=_response(requested=requested, model=model, fallback=fallback)
        )
    )
    with patch(
        "omnigent.server.task_outcome_evaluator._configured_policy_client",
        return_value=(client, None),
    ):
        outcome = await evaluate_task_outcome(pending_store, run)
    assert outcome.status == "failed"
    assert pending_store.get_evaluation_for_run(run.id) is None
    persisted = pending_store.get_run(run.id)
    assert persisted.evaluation_status == "failed"
    assert persisted.evaluation_error_kind == "provenance"
