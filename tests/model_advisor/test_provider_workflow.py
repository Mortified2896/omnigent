"""Logical provider-grouped rounds and persistence contracts."""

from __future__ import annotations

import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from omnigent.model_advisor_provider_policy import (
    LogicalChoice,
    ProviderPreferences,
    ProviderSelection,
    QualifiedRoute,
    plan_transport,
)
from omnigent.model_advisor_provider_workflow import (
    LogicalAdvisorError,
    LogicalFrozenRound,
    confirm_logical_review,
    freeze_logical_round,
    prepare_logical_review,
)
from omnigent.model_advisor_repository import AdvisorRepository, metadata

OPENAI_LOW = LogicalChoice("openai", "test-openai", "low")
OPENAI_HIGH = LogicalChoice("openai", "test-openai", "high")
GLM_HIGH = LogicalChoice("glm", "test-glm", "high")
CATALOG = (OPENAI_LOW, OPENAI_HIGH, GLM_HIGH)


def route(transport: str, choice: LogicalChoice) -> QualifiedRoute:
    return QualifiedRoute(
        choice=choice,
        transport=transport,
        route_id=f"fixture-{transport}",
        wire_model=(f"gateway/{choice.model_id}" if transport == "omniroute" else choice.model_id),
        wire_effort=choice.reasoning_effort,
        entitlement_kind="chatgpt_plan" if choice.provider == "openai" else "glm_plan",
        entitlement_key="fixture-same-plan-account",
        equivalence_key=f"fixture:{choice.provider}:{choice.model_id}:{choice.reasoning_effort}",
        catalog_revision="fixture-v1",
    )


ROUTES = {
    choice.choice_id: (route("omniroute", choice), route("direct", choice)) for choice in CATALOG
}
PREFERENCES = ProviderPreferences(
    enabled=True,
    openai=ProviderSelection(selected_choice_ids=(OPENAI_LOW.choice_id,)),
    glm=ProviderSelection(selected_choice_ids=(GLM_HIGH.choice_id,)),
    advisor_choice_id=OPENAI_HIGH.choice_id,
)


def frozen_round() -> LogicalFrozenRound:
    return freeze_logical_round(
        owner_id="owner",
        host_id="host",
        round_id="round-1",
        settings_revision="settings-1",
        task="Explain the task.",
        preferences=PREFERENCES,
        catalog=CATALOG,
        routes_by_choice=ROUTES,
        human_choice_id=GLM_HIGH.choice_id,
    )


def test_advisor_input_is_transport_neutral_and_human_independent() -> None:
    frozen = frozen_round()
    direct = replace(
        PREFERENCES,
        openai=replace(PREFERENCES.openai, transport_preference="direct_only"),
    )
    direct_round = freeze_logical_round(
        owner_id="owner",
        host_id="host",
        round_id="round-2",
        settings_revision="settings-1",
        task="Explain the task.",
        preferences=direct,
        catalog=CATALOG,
        routes_by_choice=ROUTES,
        human_choice_id=OPENAI_LOW.choice_id,
    )
    assert frozen.advisor_input() == direct_round.advisor_input()
    encoded = json.dumps(frozen.advisor_input())
    for forbidden in ("omniroute", "route_id", "entitlement_key", "fallback", "human_choice"):
        assert forbidden not in encoded


def test_advisor_engine_can_be_independent_of_answer_pool() -> None:
    frozen = frozen_round()
    assert frozen.advisor.choice_id == OPENAI_HIGH.choice_id
    assert frozen.advisor.choice_id not in {choice.choice_id for choice in frozen.pool}
    assert frozen.advisor_transport.choice == frozen.advisor
    assert LogicalFrozenRound.from_payload(frozen.to_payload()) == frozen


def test_logical_review_and_override_keep_original_assignment() -> None:
    frozen = frozen_round()
    raw = json.dumps({"candidate_id": OPENAI_LOW.choice_id, "rationale": "Lower effort fits."})
    review = prepare_logical_review(frozen, raw, randbelow=lambda _: 1)
    assert review.human_choice_id == GLM_HIGH.choice_id
    assert review.advisor_choice_id == OPENAI_LOW.choice_id
    override = confirm_logical_review(
        frozen,
        review,
        override_choice_id=OPENAI_LOW.choice_id,
        reason="Prefer the OpenAI checkpoint for this task",
    )
    assert override.original_assignment == review.original_assignment
    assert override.execution_choice_id == OPENAI_LOW.choice_id
    assert override.comparison_group == "manual_override"


def test_advisor_output_cannot_select_engine_or_physical_route() -> None:
    frozen = frozen_round()
    for raw in (
        json.dumps({"candidate_id": frozen.advisor.choice_id, "rationale": "wrong pool"}),
        json.dumps({"candidate_id": OPENAI_LOW.choice_id, "rationale": "x", "route": "direct"}),
    ):
        with pytest.raises(LogicalAdvisorError):
            prepare_logical_review(frozen, raw, randbelow=lambda _: 0)


def test_provider_repository_claims_advice_and_dispatch_once() -> None:
    frozen = frozen_round()
    with tempfile.TemporaryDirectory() as directory:
        engine = create_engine("sqlite:///" + str(Path(directory) / "advisor.db"))
        metadata.create_all(engine)
        repository = AdvisorRepository(engine)
        reserved = repository.reserve_provider_round(frozen, submission_key="retry-key")
        assert reserved.acquired
        advice = json.dumps({"candidate_id": OPENAI_LOW.choice_id, "rationale": "Fits."})
        finished = repository.finish_provider_advice(
            "owner", "host", "round-1", advice, randbelow=lambda _: 1
        )
        replay = repository.finish_provider_advice(
            "owner",
            "host",
            "round-1",
            advice,
            randbelow=lambda _: (_ for _ in ()).throw(AssertionError("redraw")),
        )
        assert finished.acquired
        assert not replay.acquired
        review = finished.record.payload["review"]
        choice = next(
            item for item in frozen.pool if item.choice_id == review["execution_choice_id"]
        )
        plan = plan_transport(choice, "omniroute_preferred", ROUTES[choice.choice_id])
        claimed = repository.confirm_provider(
            "owner",
            "host",
            "round-1",
            expected_version=finished.record.version,
            execution_plan=plan.to_payload(),
        )
        again = repository.confirm_provider(
            "owner",
            "host",
            "round-1",
            expected_version=claimed.record.version,
            execution_plan=plan.to_payload(),
        )
        assert claimed.acquired
        assert not again.acquired
        assert claimed.record.state == "dispatch_claimed"
        assert claimed.record.payload["execution_plan"] == plan.to_payload()
        assert "execution_session_id" not in claimed.record.payload
        engine.dispose()


def test_route_plan_roundtrip_rejects_another_logical_choice() -> None:
    frozen = frozen_round()
    plan = plan_transport(OPENAI_LOW, "omniroute_preferred", ROUTES[OPENAI_LOW.choice_id])
    payload = frozen.to_payload()
    payload["advisor_transport"] = plan.to_payload()
    with pytest.raises(LogicalAdvisorError):
        LogicalFrozenRound.from_payload(payload)
