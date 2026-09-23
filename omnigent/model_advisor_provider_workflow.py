"""Versioned logical-choice rounds for the provider-grouped Model Advisor.

The v1 workflow stores physical lane-bound candidates.  This module stores a
canonical provider/checkpoint/effort choice and keeps the selected transport
as a server-owned execution detail.  It deliberately shares the same review,
assignment and durable-reservation lifecycle as v1 without decoding old
rounds as logical rounds.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from omnigent.model_advisor_provider_policy import (
    LogicalChoice,
    Preference,
    ProviderPreferences,
    QualifiedRoute,
    TransportPlan,
    advisor_input,
    effective_pool,
    plan_transport,
    resolve_advisor,
)
from omnigent.model_advisor_workflow import canonical_json, document_digest, task_fingerprint


class LogicalAdvisorError(ValueError):
    """Invalid logical round, advice, assignment or route snapshot."""


Arm = Literal["human", "advisor", "same"]


def _id(value: object, *, limit: int = 256) -> None:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > limit
        or any(ord(char) < 32 for char in value)
    ):
        raise LogicalAdvisorError("Invalid logical round identifier")


def _strict_json_object(raw: str) -> dict[str, object]:
    def pairs(values: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in values:
            if key in result:
                raise LogicalAdvisorError("Advisor output contains a duplicate property")
            result[key] = value
        return result

    try:
        value = json.loads(raw, object_pairs_hook=pairs)
    except (json.JSONDecodeError, RecursionError) as exc:
        raise LogicalAdvisorError("Advisor did not return a JSON selection") from exc
    if not isinstance(value, dict):
        raise LogicalAdvisorError("Advisor output must be a JSON object")
    return value


def parse_logical_advisor_result(
    raw: str, pool: tuple[LogicalChoice, ...]
) -> tuple[LogicalChoice, str]:
    """Parse one candidate id and rationale against the frozen logical pool."""
    if not isinstance(raw, str) or len(raw.encode("utf-8")) > 8192:
        raise LogicalAdvisorError("Invalid advisor output size")
    result = _strict_json_object(raw)
    if set(result) != {"candidate_id", "rationale"}:
        raise LogicalAdvisorError("Advisor output must contain only candidate_id and rationale")
    candidate_id = result["candidate_id"]
    rationale = result["rationale"]
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise LogicalAdvisorError("Advisor returned an invalid logical choice id")
    if not isinstance(rationale, str) or not rationale.strip() or len(rationale) > 600:
        raise LogicalAdvisorError("Advisor returned an invalid rationale")
    for choice in pool:
        if choice.choice_id == candidate_id:
            return choice, rationale
    raise LogicalAdvisorError("Advisor selected a choice outside the frozen pool")


@dataclass(frozen=True)
class LogicalFrozenRound:
    """A v2 round whose semantic choices contain no transport identity."""

    owner_id: str
    host_id: str
    round_id: str
    settings_revision: str
    task: str
    pool: tuple[LogicalChoice, ...]
    advisor: LogicalChoice
    human_choice_id: str
    human_probability_percent: int
    transport_preferences: tuple[tuple[str, Preference], ...]
    advisor_transport: TransportPlan
    schema_version: Literal[2] = 2

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.host_id, self.round_id, self.settings_revision):
            _id(value)
        task_fingerprint(self.task)
        if not isinstance(self.pool, tuple) or not 1 <= len(self.pool) <= 128:
            raise LogicalAdvisorError("Logical pool must be nonempty and bounded")
        if any(not isinstance(choice, LogicalChoice) for choice in self.pool):
            raise LogicalAdvisorError("Invalid logical pool choice")
        if len({choice.choice_id for choice in self.pool}) != len(self.pool):
            raise LogicalAdvisorError("Duplicate logical pool choice")
        choices = {choice.choice_id: choice for choice in self.pool}
        if self.human_choice_id not in choices:
            raise LogicalAdvisorError("Human proposal is outside the frozen logical pool")
        if self.advisor_transport.choice != self.advisor:
            raise LogicalAdvisorError("Advisor route is for another logical choice")
        if (
            type(self.human_probability_percent) is not int
            or not 0 <= self.human_probability_percent <= 100
        ):
            raise LogicalAdvisorError("Invalid assignment probability")
        if self.schema_version != 2:
            raise LogicalAdvisorError("Unsupported logical round version")
        if len(self.transport_preferences) != 2 or {
            provider for provider, _ in self.transport_preferences
        } != {"openai", "glm"}:
            raise LogicalAdvisorError("Both provider connection preferences must be frozen")
        for provider, preference in self.transport_preferences:
            if provider not in {"openai", "glm"} or preference not in {
                "omniroute_preferred",
                "direct_only",
            }:
                raise LogicalAdvisorError("Invalid frozen connection preference")

    @property
    def fingerprint(self) -> str:
        return document_digest({"policy_version": "visible-advisor-v2", **self.to_payload()})

    def advisor_input(self) -> dict[str, object]:
        # Routes, account keys, preferences and the human proposal are absent
        # by construction.  Transport is resolved after the logical proposal.
        return advisor_input(self.task, self.pool)

    def preference_for(self, provider: str) -> Preference:
        for name, preference in self.transport_preferences:
            if name == provider:
                return preference
        raise LogicalAdvisorError("Missing frozen provider preference")

    def to_payload(self) -> dict[str, object]:
        return {
            "schema_version": 2,
            "owner_id": self.owner_id,
            "host_id": self.host_id,
            "round_id": self.round_id,
            "settings_revision": self.settings_revision,
            "task": self.task,
            "pool": [
                {
                    "provider": choice.provider,
                    "model_id": choice.model_id,
                    "reasoning_effort": choice.reasoning_effort,
                }
                for choice in self.pool
            ],
            "advisor": {
                "provider": self.advisor.provider,
                "model_id": self.advisor.model_id,
                "reasoning_effort": self.advisor.reasoning_effort,
            },
            "human_choice_id": self.human_choice_id,
            "human_probability_percent": self.human_probability_percent,
            "transport_preferences": [list(item) for item in self.transport_preferences],
            "advisor_transport": self.advisor_transport.to_payload(),
        }

    @classmethod
    def from_payload(cls, payload: dict) -> LogicalFrozenRound:
        if not isinstance(payload, dict) or payload.get("schema_version") != 2:
            raise LogicalAdvisorError("Expected a v2 logical frozen round")
        raw_pool = payload.get("pool")
        if not isinstance(raw_pool, list):
            raise LogicalAdvisorError("Invalid logical frozen pool")
        pool = tuple(LogicalChoice(**choice) for choice in raw_pool if isinstance(choice, dict))
        if len(pool) != len(raw_pool):
            raise LogicalAdvisorError("Invalid logical frozen choice")
        raw_preferences = payload.get("transport_preferences")
        if not isinstance(raw_preferences, list):
            raise LogicalAdvisorError("Invalid frozen connection preferences")
        preferences = tuple(
            (str(item[0]), str(item[1]))
            for item in raw_preferences
            if isinstance(item, list) and len(item) == 2
        )
        if len(preferences) != len(raw_preferences):
            raise LogicalAdvisorError("Invalid frozen connection preference")
        advisor = LogicalChoice(**payload["advisor"])
        return cls(
            owner_id=payload["owner_id"],
            host_id=payload["host_id"],
            round_id=payload["round_id"],
            settings_revision=payload["settings_revision"],
            task=payload["task"],
            pool=pool,
            advisor=advisor,
            human_choice_id=payload["human_choice_id"],
            human_probability_percent=payload["human_probability_percent"],
            transport_preferences=preferences,
            advisor_transport=TransportPlan.from_payload(payload["advisor_transport"]),
        )


def freeze_logical_round(
    *,
    owner_id: str,
    host_id: str,
    round_id: str,
    settings_revision: str,
    task: str,
    preferences: ProviderPreferences,
    catalog: tuple[LogicalChoice, ...],
    routes_by_choice: dict[str, tuple[QualifiedRoute, ...]],
    human_choice_id: str,
) -> LogicalFrozenRound:
    """Freeze the logical pool and advisor route before the single call."""
    pool = effective_pool(preferences, catalog)
    by_id = {choice.choice_id: choice for choice in pool}
    if human_choice_id not in by_id:
        raise LogicalAdvisorError("Human choice is outside the active logical answer pool")
    advisor = resolve_advisor(preferences, catalog)
    advisor_routes = routes_by_choice.get(advisor.choice_id, ())
    advisor_plan = plan_transport(
        advisor,
        preferences.group(advisor.provider).transport_preference,
        advisor_routes,
    )
    return LogicalFrozenRound(
        owner_id=owner_id,
        host_id=host_id,
        round_id=round_id,
        settings_revision=settings_revision,
        task=task,
        pool=pool,
        advisor=advisor,
        human_choice_id=human_choice_id,
        human_probability_percent=preferences.human_probability_percent,
        transport_preferences=(
            ("openai", preferences.openai.transport_preference),
            ("glm", preferences.glm.transport_preference),
        ),
        advisor_transport=advisor_plan,
    )


@dataclass(frozen=True)
class LogicalAssignment:
    round_fingerprint: str
    arm: Arm
    selected_choice_id: str
    probability_numerator: int
    probability_denominator: int


@dataclass(frozen=True)
class LogicalReviewDecision:
    """Visible logical proposals and the original assignment."""

    round_fingerprint: str
    human_choice_id: str
    advisor_choice_id: str
    rationale: str
    original_assignment: LogicalAssignment
    execution_choice_id: str
    human_probability_percent: int
    overridden: bool = False
    override_reason: str | None = None
    recommendation_visible: Literal[True] = True

    @property
    def comparison_group(self) -> str:
        if self.overridden:
            return "manual_override"
        if self.original_assignment.arm == "same":
            return "agreement"
        if self.human_probability_percent in (0, 100):
            return "deterministic"
        return "randomized_unblinded"

    def to_payload(self) -> dict[str, object]:
        return json.loads(
            canonical_json(
                {
                    "round_fingerprint": self.round_fingerprint,
                    "human_choice_id": self.human_choice_id,
                    "advisor_choice_id": self.advisor_choice_id,
                    "rationale": self.rationale,
                    "original_assignment": {
                        "round_fingerprint": self.original_assignment.round_fingerprint,
                        "arm": self.original_assignment.arm,
                        "selected_choice_id": self.original_assignment.selected_choice_id,
                        "probability_numerator": self.original_assignment.probability_numerator,
                        "probability_denominator": (
                            self.original_assignment.probability_denominator
                        ),
                    },
                    "execution_choice_id": self.execution_choice_id,
                    "human_probability_percent": self.human_probability_percent,
                    "overridden": self.overridden,
                    "override_reason": self.override_reason,
                    "recommendation_visible": self.recommendation_visible,
                    "comparison_group": self.comparison_group,
                }
            )
        )

    @classmethod
    def from_payload(cls, payload: dict) -> LogicalReviewDecision:
        data = dict(payload)
        data.pop("comparison_group", None)
        data["original_assignment"] = LogicalAssignment(**data["original_assignment"])
        return cls(**data)


def prepare_logical_review(
    frozen: LogicalFrozenRound,
    raw_advice: str,
    *,
    randbelow: Callable[[int], int],
) -> LogicalReviewDecision:
    advisor_pick, rationale = parse_logical_advisor_result(raw_advice, frozen.pool)
    same = frozen.human_choice_id == advisor_pick.choice_id
    pct = frozen.human_probability_percent
    if same:
        assignment = LogicalAssignment(frozen.fingerprint, "same", advisor_pick.choice_id, 1, 1)
    elif pct == 50:
        bit = randbelow(2)
        if type(bit) is not int or bit not in (0, 1):
            raise LogicalAdvisorError("Assignment draw must be zero or one")
        arm: Arm = "human" if bit == 0 else "advisor"
        selected = frozen.human_choice_id if arm == "human" else advisor_pick.choice_id
        assignment = LogicalAssignment(frozen.fingerprint, arm, selected, 1, 2)
    elif pct in (0, 100):
        arm = "human" if pct == 100 else "advisor"
        selected = frozen.human_choice_id if arm == "human" else advisor_pick.choice_id
        assignment = LogicalAssignment(frozen.fingerprint, arm, selected, 1, 1)
    else:
        draw = randbelow(100)
        if type(draw) is not int or not 0 <= draw < 100:
            raise LogicalAdvisorError("Assignment draw must be an integer from 0 to 99")
        human = draw < pct
        arm = "human" if human else "advisor"
        selected = frozen.human_choice_id if human else advisor_pick.choice_id
        assignment = LogicalAssignment(
            frozen.fingerprint,
            arm,
            selected,
            pct if human else 100 - pct,
            100,
        )
    return LogicalReviewDecision(
        round_fingerprint=frozen.fingerprint,
        human_choice_id=frozen.human_choice_id,
        advisor_choice_id=advisor_pick.choice_id,
        rationale=rationale,
        original_assignment=assignment,
        execution_choice_id=assignment.selected_choice_id,
        human_probability_percent=pct,
    )


def confirm_logical_review(
    frozen: LogicalFrozenRound,
    review: LogicalReviewDecision,
    *,
    override_choice_id: str | None = None,
    reason: str | None = None,
) -> LogicalReviewDecision:
    """Apply a visible override while retaining the original assignment."""
    if review.round_fingerprint != frozen.fingerprint:
        raise LogicalAdvisorError("Review belongs to another logical round")
    choice_ids = {choice.choice_id for choice in frozen.pool}
    chosen = review.execution_choice_id
    if chosen not in choice_ids:
        raise LogicalAdvisorError("Assigned logical choice left the frozen pool")
    if override_choice_id is not None:
        if override_choice_id not in choice_ids:
            raise LogicalAdvisorError("Override is outside the frozen logical pool")
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
            raise LogicalAdvisorError("An override needs a short reason")
        return replace(
            review,
            execution_choice_id=override_choice_id,
            overridden=True,
            override_reason=reason.strip(),
        )
    return review
