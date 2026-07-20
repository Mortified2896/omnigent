"""Tests for the task-outcome HTTP routes.

Exercises the request/response bodies against an in-memory
SQLAlchemy store. Auth is bypassed (single-user mode) so the
tests focus on the store wiring + payload shape, not the
permission plumbing.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from starlette.responses import JSONResponse

from omnigent.db.utils import get_or_create_engine
from omnigent.errors import OmnigentError
from omnigent.server.routes.task_outcomes import create_task_outcomes_router
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.task_outcome_store import (
    CreateRoutingDecisionInput,
    CreateRoutingProposalInput,
    CreateTaskRunInput,
    TaskOutcomeStore,
    UpdateTaskRunTerminalInput,
)
from omnigent.stores.task_outcome_store.sqlalchemy_store import (
    SqlAlchemyTaskOutcomeStore,
)


@pytest.fixture
def store_and_app(tmp_path_factory) -> tuple[TaskOutcomeStore, TestClient]:
    """Build a (store, TestClient) pair sharing one SQLite DB.

    Mirrors the production setup by also registering the
    ``OmnigentError`` exception handler on the test app — the
    routes raise ``OmnigentError`` for 400/403/404 and the test
    client needs the handler installed to surface the JSON error
    body that the production clients see.
    """
    db_path = tmp_path_factory.mktemp("route") / "test.db"
    uri = f"sqlite:///{db_path}"
    engine = get_or_create_engine(uri)
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO conversations (id, created_at, updated_at, "
                "kind, root_conversation_id) VALUES ('c1', 1, 1, 1, 'c1')"
            )
        )
    store: TaskOutcomeStore = SqlAlchemyTaskOutcomeStore(uri)
    conv_store = SqlAlchemyConversationStore(uri)
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_omnigent_error(request, exc):
        return JSONResponse(
            status_code=exc.http_status,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    app.include_router(
        create_task_outcomes_router(
            store,
            conversation_store=conv_store,
            auth_provider=None,
            permission_store=None,
        ),
        prefix="/v1",
    )
    return store, TestClient(app)


def _seed_terminalised(
    store: TaskOutcomeStore,
    *,
    conv: str = "c1",
    status: str = "completed",
    response_id: str | None = None,
) -> str:
    """Create + terminalise a run via the store; return its id."""
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id=conv,
            response_id=response_id or f"r_{secrets.token_hex(8)}",
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
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status=status,
            terminal_at=200,
            input_tokens=1000,
            output_tokens=200,
            total_cost_usd=0.02,
            response_summary="Fixed it.",
            changed_files=["src/auth.py"],
            commit_sha="abc1234567",
        )
    )
    return run.id


# ── by-response task-run lookup ─────────────────────────────────────────


def test_get_task_run_by_response_requires_exact_transcript_response_id(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """An unrelated assistant response cannot adopt the sole terminal run."""
    store, client = store_and_app
    _seed_terminalised(store, response_id="resp_real_task")

    exact = client.get("/v1/sessions/c1/task-runs/by-response/resp_real_task")
    assert exact.status_code == 200
    assert exact.json()["run"]["response_id"] == "resp_real_task"

    unmatched = client.get("/v1/sessions/c1/task-runs/by-response/resp_runner_disconnected")
    assert unmatched.status_code == 404


# ── list_session_task_runs ──────────────────────────────────────────────


def test_list_session_task_runs_returns_summary(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``GET /v1/sessions/{id}/task-runs`` returns the summary shape."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.get("/v1/sessions/c1/task-runs?limit=10")
    assert response.status_code == 200
    body = response.json()
    assert body["object"] == "list"
    assert any(r["id"] == run_id for r in body["runs"])


def test_list_session_task_runs_returns_empty_for_unknown_session(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """Unknown session returns ``{"runs": []}`` (not 404)."""
    _store, client = store_and_app
    response = client.get("/v1/sessions/unknown-session/task-runs")
    assert response.status_code == 200
    assert response.json() == {"object": "list", "runs": []}


def test_list_session_routing_turns_returns_durable_decision_and_run_links(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    store, client = store_and_app
    proposal = store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id="c1",
            elicitation_id="route_1",
            user_message="fix it",
            content_types=["input_text"],
            original_route_id="auto/coding",
            original_harness="OpenCode Native",
            original_reasoning_effort="medium",
            original_permission_mode="ask_before_edits",
            requires_explicit_approval=False,
            evaluator_route_id="auto/smart",
            evaluator_provider="mistral",
            evaluator_model="mistral-large",
            evaluator_decision_id="eval-decision",
            proposal_payload={
                "omniroute_route_id": "auto/coding",
                "reasoning_effort": "medium",
                "permission_mode": "ask_before_edits",
            },
        )
    )
    decision = store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=proposal.id,
            action="changed",
            decision_payload={"action": "accept"},
            final_harness="OpenCode Native",
            final_route_id="auto/coding:reliable",
            final_reasoning_effort="high",
            final_permission_mode="read_only",
        )
    )
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="resp_1",
            triggering_message_id="msg_user_1",
            routing_proposal_id=proposal.id,
            routing_decision_id=decision.id,
        )
    )
    declined_proposal = store.create_routing_proposal(
        CreateRoutingProposalInput(
            conversation_id="c1",
            elicitation_id="route_2",
            user_message="do not run",
            content_types=["input_text"],
            original_route_id="auto/coding",
            requires_explicit_approval=False,
            proposal_payload={"omniroute_route_id": "auto/coding"},
        )
    )
    store.create_routing_decision(
        CreateRoutingDecisionInput(
            proposal_id=declined_proposal.id,
            action="declined",
            decision_payload={"action": "decline"},
        )
    )

    response = client.get("/v1/sessions/c1/routing-turns")

    assert response.status_code == 200
    turns = {turn["id"]: turn for turn in response.json()["turns"]}
    turn = turns[proposal.id]
    assert turn["id"] == proposal.id
    assert turn["action"] == "accept"
    assert turn["durable_action"] == "changed"
    assert turn["response"] == {
        "action": "accept",
        "content": {"final_selection": turn["final_selection"]},
    }
    assert turn["original_selection"]["omniroute_route_id"] == "auto/coding"
    assert turn["final_selection"]["omniroute_route_id"] == "auto/coding:reliable"
    assert turn["evaluator_provenance"]["decision_id"] == "eval-decision"
    assert turn["triggering_message_id"] == "msg_user_1"
    assert turn["response_id"] == "resp_1"
    assert turn["proposal"]["eligible_route_ids"]
    assert turn["proposal"]["eligible_reasoning_efforts"]
    assert turn["proposal"]["eligible_permission_modes"]
    assert run.routing_proposal_id == proposal.id
    assert turns[declined_proposal.id]["action"] == "decline"
    assert turns[declined_proposal.id]["durable_action"] == "declined"


# ── get_task_run ────────────────────────────────────────────────────────


def test_get_task_run_returns_aggregate(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``GET /v1/task-runs/{id}`` returns the run + evaluation + review + langfuse_pending."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.get(f"/v1/task-runs/{run_id}")
    assert response.status_code == 200
    body = response.json()
    assert body["run"]["id"] == run_id
    assert body["run"]["terminal_status"] == "completed"
    assert body["evaluation"] is None
    assert body["review"] is None
    assert body["langfuse_pending"] is False


def test_get_task_run_404_when_missing(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``GET /v1/task-runs/nonexistent`` returns 404."""
    _store, client = store_and_app
    response = client.get("/v1/task-runs/nonexistent")
    assert response.status_code == 404


def test_get_direct_exact_model_run_returns_selection_context(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    store, client = store_and_app
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="resp_direct_model",
            harness_id="opencode-native",
            selected_provider="openai",
            selected_model="openai/gpt-5.4",
        )
    )
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=200,
        )
    )

    response = client.get(f"/v1/task-runs/{run.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["routing"] is None
    assert body["selection"] == {
        "source": "user_selected_model",
        "requested": {
            "harness": "opencode-native",
            "provider": "openai",
            "model": "openai/gpt-5.4",
            "route_id": None,
            "reasoning_effort": None,
            "permission_mode": None,
        },
    }


def test_get_direct_omniroute_run_returns_selection_context(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    store, client = store_and_app
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="resp_direct_route",
            harness_id="opencode-native",
            requested_route_id="auto/coding:reliable",
            selected_provider="omniroute",
            selected_model="auto/coding:reliable",
        )
    )
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=200,
        )
    )

    response = client.get(f"/v1/task-runs/{run.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["routing"] is None
    assert body["selection"] == {
        "source": "user_selected_route",
        "requested": {
            "harness": "opencode-native",
            "provider": "omniroute",
            "model": "auto/coding:reliable",
            "route_id": "auto/coding:reliable",
            "reasoning_effort": None,
            "permission_mode": None,
        },
    }


def test_submit_direct_review_does_not_require_routing_links(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    store, client = store_and_app
    run = store.create_run(
        CreateTaskRunInput(
            conversation_id="c1",
            response_id="resp_direct_review",
            harness_id="opencode-native",
            selected_provider="openai",
            selected_model="openai/gpt-5.4",
        )
    )
    store.update_run_terminal(
        UpdateTaskRunTerminalInput(
            task_run_id=run.id,
            terminal_status="completed",
            terminal_at=200,
        )
    )

    response = client.post(
        f"/v1/task-runs/{run.id}/review",
        json={"action": "adjust", "verdict": "success", "quality_score": 5},
    )

    assert response.status_code == 200
    assert response.json()["verdict"] == "success"
    detail = client.get(f"/v1/task-runs/{run.id}").json()
    assert detail["routing"] is None
    assert detail["review"]["id"] == response.json()["id"]


# ── submit_task_run_review ──────────────────────────────────────────────


def test_submit_review_creates_row(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``POST /v1/task-runs/{id}/review`` creates the review row."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={
            "verdict": "success",
            "quality_score": 5,
            "final_task_family": "small_bug_fix",
            "evaluator_accuracy": "correct",
            "comments": "Looks great",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "success"
    assert body["quality_score"] == 5
    assert body["final_task_family"] == "small_bug_fix"
    assert body["evaluator_accuracy"] == "correct"
    # Single-user mode: created_by is None (matches the comments
    # store's attribution convention).
    assert body["created_by"] is None


def test_submit_review_re_submits_in_place(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """A second POST with the same created_by UPDATES the row in place."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    first = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"verdict": "success", "comments": "first"},
    ).json()
    second = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"verdict": "partial", "comments": "second"},
    ).json()
    assert first["id"] == second["id"], "upsert must update in place"
    assert second["verdict"] == "partial"
    assert second["comments"] == "second"


def test_submit_review_rejects_unknown_verdict(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``verdict`` outside the vocabulary → 400 INVALID_INPUT."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"verdict": "garbage"},
    )
    assert response.status_code == 400
    # The handler returns ``{"error": {"code": ..., "message": ...}}``.
    body = response.json()
    assert "verdict" in body["error"]["message"]


def test_submit_review_rejects_unknown_task_family(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``final_task_family`` outside the vocabulary → 400."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={
            "verdict": "success",
            "final_task_family": "not_a_real_family",
        },
    )
    assert response.status_code == 400


def test_submit_review_rejects_unknown_evaluator_accuracy(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``evaluator_accuracy`` outside the vocabulary → 400."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={
            "verdict": "success",
            "evaluator_accuracy": "not_an_accuracy",
        },
    )
    assert response.status_code == 400


def test_submit_review_rejects_out_of_range_quality(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``quality_score`` outside 1..5 → 422 (Pydantic validation)."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"verdict": "success", "quality_score": 99},
    )
    assert response.status_code == 422


def test_decline_persists_negative_non_learning_review(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"action": "decline"},
    )
    assert response.status_code == 200
    assert response.json()["review_action"] == "declined"
    assert response.json()["verdict"] == "skipped"
    assert response.json()["learning_eligible"] is False
    persisted = store.get_any_review_for_run(run_id)
    assert persisted is not None
    assert persisted.review_action == "declined"
    listing = client.get("/v1/sessions/c1/unreviewed-task-outcomes")
    assert run_id not in listing.json()["task_run_ids"]


def test_submit_review_skip_is_a_real_state(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``verdict='skipped'`` is a persisted state — distinct from "no review"."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    response = client.post(
        f"/v1/task-runs/{run_id}/review",
        json={"verdict": "skipped"},
    )
    assert response.status_code == 200
    body = response.json()
    assert body["verdict"] == "skipped"


# ── unreviewed listing ──────────────────────────────────────────────────


def test_unreviewed_task_outcomes_filters_reviewed(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``GET /v1/sessions/{id}/unreviewed-task-outcomes`` excludes reviewed runs."""
    store, client = store_and_app
    run_unreviewed = _seed_terminalised(store)
    run_reviewed = _seed_terminalised(store)
    client.post(f"/v1/task-runs/{run_reviewed}/review", json={"verdict": "success"})
    response = client.get("/v1/sessions/c1/unreviewed-task-outcomes")
    assert response.status_code == 200
    body = response.json()
    assert body["task_run_ids"] == [run_unreviewed]


def test_unreviewed_task_outcomes_includes_skipped(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """``skipped`` is a real review, so it drops out of the unreviewed list."""
    store, client = store_and_app
    run_id = _seed_terminalised(store)
    client.post(f"/v1/task-runs/{run_id}/review", json={"verdict": "skipped"})
    response = client.get("/v1/sessions/c1/unreviewed-task-outcomes")
    body = response.json()
    assert body["task_run_ids"] == []


def test_unreviewed_task_outcomes_excludes_running_runs(
    store_and_app: tuple[TaskOutcomeStore, TestClient],
) -> None:
    """A still-running task is not reviewable."""
    _store, client = store_and_app
    # Seed a still-running run via raw SQL (the store's create_run
    # defaults to "running" already; the route filter checks status).
    run_id = f"tr_{secrets.token_hex(16)}"
    from omnigent.db.utils import _engine_cache  # type: ignore[attr-defined]

    uri = next(uri for uri in _engine_cache if "test.db" in uri)
    with get_or_create_engine(uri).begin() as conn:
        conn.execute(
            text(
                "INSERT INTO task_runs (workspace_id, id, conversation_id, "
                "terminal_status, started_at, created_at, updated_at) "
                "VALUES (0, :rid, 'c1', 1, 100, 100, 100)"
            ),
            {"rid": run_id},
        )
    response = client.get("/v1/sessions/c1/unreviewed-task-outcomes")
    body = response.json()
    assert run_id not in body["task_run_ids"]
