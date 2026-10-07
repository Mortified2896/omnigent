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
from typing import Literal, cast

from omnigent.model_advisor_provider_policy import (
    ExclusionReason,
    LogicalChoice,
    Preference,
    ProviderPreferences,
    QualifiedRoute,
    TransportPlan,
    advisor_input,
    plan_transport,
    resolve_advisor,
    selection_snapshot,
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
    """A frozen logical round; v3 adds immutable decision-context evidence."""

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
    schema_version: Literal[2, 3] = 3
    catalog_revision: str | None = None
    qualified_pool: tuple[LogicalChoice, ...] = ()
    user_enabled_pool: tuple[LogicalChoice, ...] = ()
    excluded_choices: tuple[tuple[str, ExclusionReason], ...] = ()
    preferences_snapshot: ProviderPreferences | None = None
    current_execution: dict[str, str | None] | None = None
    continuation_session_id: str | None = None
    keep_chosen_model: bool = True
    task_tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.host_id, self.round_id, self.settings_revision):
            _id(value)
        if self.continuation_session_id is not None:
            _id(self.continuation_session_id)
        if type(self.keep_chosen_model) is not bool:
            raise LogicalAdvisorError("Invalid continuation policy")
        if (
            not isinstance(self.task_tags, tuple)
            or len(self.task_tags) > 8
            or any(
                not isinstance(tag, str) or not tag.strip() or len(tag) > 40
                for tag in self.task_tags
            )
        ):
            raise LogicalAdvisorError("Invalid task tags")
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
        if self.schema_version not in (2, 3):
            raise LogicalAdvisorError("Unsupported logical round version")
        if len(self.transport_preferences) != 2 or {
            provider for provider, _ in self.transport_preferences
        } != {"openai", "glm"}:
            raise LogicalAdvisorError("Both provider connection preferences must be frozen")
        for provider, preference in self.transport_preferences:
            if provider not in {"openai", "glm"} or preference not in {
                "omniroute_preferred",
                "omniroute_only",
                "direct_only",
            }:
                raise LogicalAdvisorError("Invalid frozen connection preference")
        if self.schema_version == 2:
            if (
                self.catalog_revision is not None
                or self.qualified_pool
                or self.user_enabled_pool
                or self.excluded_choices
                or self.preferences_snapshot is not None
            ):
                raise LogicalAdvisorError("A v2 frozen round cannot carry v3 decision context")
        else:
            _id(self.catalog_revision)
            if not isinstance(self.preferences_snapshot, ProviderPreferences):
                raise LogicalAdvisorError("V3 frozen round requires a preferences snapshot")
            if self.preferences_snapshot.to_payload().get("schema_version") != 3:
                raise LogicalAdvisorError("V3 frozen round requires schema-v3 preferences")
            for collection, label in (
                (self.qualified_pool, "qualified"),
                (self.user_enabled_pool, "user-enabled"),
            ):
                if not isinstance(collection, tuple) or not collection:
                    raise LogicalAdvisorError(f"V3 round requires a nonempty {label} pool")
                if any(not isinstance(choice, LogicalChoice) for choice in collection):
                    raise LogicalAdvisorError(f"Invalid {label} choice snapshot")
                if len({choice.choice_id for choice in collection}) != len(collection):
                    raise LogicalAdvisorError(f"Duplicate {label} choice snapshot")
            qualified_ids = {choice.choice_id for choice in self.qualified_pool}
            user_enabled_ids = {choice.choice_id for choice in self.user_enabled_pool}
            visible_ids = {choice.choice_id for choice in self.pool}
            if not user_enabled_ids <= qualified_ids:
                raise LogicalAdvisorError("User-enabled choices exceed the qualified catalog")
            if visible_ids != user_enabled_ids:
                raise LogicalAdvisorError("Advisor-visible pool must match user-enabled choices")
            if not visible_ids <= qualified_ids:
                raise LogicalAdvisorError("Advisor-visible choices exceed the qualified catalog")
            if self.advisor.choice_id not in qualified_ids:
                raise LogicalAdvisorError("Advisor executor is outside the qualified catalog")
            if any(
                reason
                not in {
                    "provider_disabled",
                    "model_disabled",
                    "reasoning_not_selected",
                    "unavailable_from_live_catalog",
                }
                for _choice_id, reason in self.excluded_choices
            ):
                raise LogicalAdvisorError("Invalid frozen choice exclusion reason")
            exclusion_ids = [choice_id for choice_id, _reason in self.excluded_choices]
            if len(set(exclusion_ids)) != len(exclusion_ids):
                raise LogicalAdvisorError("Duplicate frozen choice exclusion")

    @property
    def fingerprint(self) -> str:
        policy = "visible-advisor-v2" if self.schema_version == 2 else "visible-advisor-v3"
        return document_digest({"policy_version": policy, **self.to_payload()})

    @property
    def visible_pool_digest(self) -> str:
        return document_digest(sorted(choice.choice_id for choice in self.pool))

    def trace_attributes(self) -> dict[str, str | int]:
        """Compact, bounded span attributes; full snapshots stay in the DB."""
        if self.schema_version != 3:
            return {}
        return {
            "advisor.catalog_revision": self.catalog_revision or "",
            "advisor.settings_revision": self.settings_revision,
            "advisor.settings_schema_version": 3,
            "advisor.qualified_pool_size": len(self.qualified_pool),
            "advisor.visible_pool_size": len(self.pool),
            "advisor.visible_pool_digest": self.visible_pool_digest,
            "advisor.human_choice_id": self.human_choice_id,
            "advisor.executor_choice_id": self.advisor.choice_id,
            "advisor.executor_model_id": self.advisor.model_id,
        }

    def decision_context_payload(self) -> dict[str, object] | None:
        """Return the inspectable non-secret projection for new v3 rounds."""
        if self.schema_version != 3 or self.preferences_snapshot is None:
            return None

        def snapshot(choice: LogicalChoice) -> dict[str, str]:
            return {
                "choice_id": choice.choice_id,
                "provider": choice.provider,
                "model_id": choice.model_id,
                "reasoning_effort": choice.reasoning_effort,
            }

        qualified_by_id = {choice.choice_id: choice for choice in self.qualified_pool}
        excluded = []
        for choice_id, reason in self.excluded_choices:
            choice = qualified_by_id.get(choice_id)
            entry: dict[str, object] = {"choice_id": choice_id, "reason": reason}
            if choice is not None:
                entry["choice"] = snapshot(choice)
            excluded.append(entry)
        return {
            "schema_version": 3,
            "catalog_revision": self.catalog_revision,
            "settings_revision": self.settings_revision,
            "preferences_snapshot": self.preferences_snapshot.to_payload(),
            "qualified_choice_ids": sorted(qualified_by_id),
            "qualified_pool": [snapshot(choice) for choice in self.qualified_pool],
            "user_enabled_choice_ids": sorted(
                choice.choice_id for choice in self.user_enabled_pool
            ),
            "user_enabled_pool": [snapshot(choice) for choice in self.user_enabled_pool],
            "advisor_visible_choice_ids": sorted(choice.choice_id for choice in self.pool),
            "advisor_visible_pool": [snapshot(choice) for choice in self.pool],
            "excluded_choices": excluded,
            "visible_pool_digest": self.visible_pool_digest,
            "human_choice_id": self.human_choice_id,
            "advisor_executor_choice_id": self.advisor.choice_id,
            "advisor_executor_model_id": self.advisor.model_id,
        }

    def advisor_input(self) -> dict[str, object]:
        # Routes, account keys, preferences and the human proposal are absent
        # by construction.  Transport is resolved after the logical proposal.
        request = advisor_input(self.task, self.pool)
        if self.current_execution is not None:
            request["current_execution"] = self.current_execution
        return request

    def preference_for(self, provider: str) -> Preference:
        for name, preference in self.transport_preferences:
            if name == provider:
                return preference
        raise LogicalAdvisorError("Missing frozen provider preference")

    def to_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": self.schema_version,
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
        if self.schema_version == 3:
            payload.update(
                {
                    "catalog_revision": self.catalog_revision,
                    "qualified_pool": [
                        {
                            "provider": choice.provider,
                            "model_id": choice.model_id,
                            "reasoning_effort": choice.reasoning_effort,
                        }
                        for choice in self.qualified_pool
                    ],
                    "user_enabled_pool": [
                        {
                            "provider": choice.provider,
                            "model_id": choice.model_id,
                            "reasoning_effort": choice.reasoning_effort,
                        }
                        for choice in self.user_enabled_pool
                    ],
                    "excluded_choices": [
                        {"choice_id": choice_id, "reason": reason}
                        for choice_id, reason in self.excluded_choices
                    ],
                    "preferences_snapshot": self.preferences_snapshot.to_payload()
                    if self.preferences_snapshot is not None
                    else None,
                }
            )
        if self.continuation_session_id is not None:
            payload["continuation"] = {
                "session_id": self.continuation_session_id,
                "keep_chosen_model": self.keep_chosen_model,
            }
        if self.current_execution is not None:
            payload["current_execution"] = self.current_execution
        if self.task_tags:
            payload["task_tags"] = list(self.task_tags)
        return payload

    @classmethod
    def from_payload(cls, payload: dict) -> LogicalFrozenRound:
        if not isinstance(payload, dict) or payload.get("schema_version") not in (2, 3):
            raise LogicalAdvisorError("Expected a v2 or v3 logical frozen round")
        version = payload["schema_version"]
        common_keys = {
            "schema_version",
            "owner_id",
            "host_id",
            "round_id",
            "settings_revision",
            "task",
            "pool",
            "advisor",
            "human_choice_id",
            "human_probability_percent",
            "transport_preferences",
            "advisor_transport",
        }
        extra_keys = (
            {
                "catalog_revision",
                "qualified_pool",
                "user_enabled_pool",
                "excluded_choices",
                "preferences_snapshot",
            }
            if version == 3
            else set()
        )
        optional = {
            key for key in ("continuation", "task_tags", "current_execution") if key in payload
        }
        if set(payload) != common_keys | extra_keys | optional:
            raise LogicalAdvisorError("Unexpected logical frozen round shape")
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
            (str(item[0]), cast(Preference, str(item[1])))
            for item in raw_preferences
            if isinstance(item, list) and len(item) == 2
        )
        if len(preferences) != len(raw_preferences):
            raise LogicalAdvisorError("Invalid frozen connection preference")
        advisor = LogicalChoice(**payload["advisor"])
        qualified_pool: tuple[LogicalChoice, ...] = ()
        user_enabled_pool: tuple[LogicalChoice, ...] = ()
        excluded_choices: tuple[tuple[str, ExclusionReason], ...] = ()
        preferences_snapshot = None
        if version == 3:
            raw_qualified = payload["qualified_pool"]
            raw_user_enabled = payload["user_enabled_pool"]
            raw_excluded = payload["excluded_choices"]
            if not isinstance(raw_qualified, list) or not isinstance(raw_user_enabled, list):
                raise LogicalAdvisorError("Invalid v3 qualified or enabled choice snapshot")
            if not isinstance(raw_excluded, list):
                raise LogicalAdvisorError("Invalid v3 excluded choice list")
            qualified_pool = tuple(
                LogicalChoice(**choice) for choice in raw_qualified if isinstance(choice, dict)
            )
            user_enabled_pool = tuple(
                LogicalChoice(**choice) for choice in raw_user_enabled if isinstance(choice, dict)
            )
            if len(qualified_pool) != len(raw_qualified) or len(user_enabled_pool) != len(
                raw_user_enabled
            ):
                raise LogicalAdvisorError("Invalid v3 logical choice snapshot")
            parsed_excluded = []
            for value in raw_excluded:
                if not isinstance(value, dict) or set(value) != {"choice_id", "reason"}:
                    raise LogicalAdvisorError("Invalid v3 choice exclusion")
                _id(value["choice_id"])
                parsed_excluded.append((value["choice_id"], value["reason"]))
            excluded_choices = tuple(parsed_excluded)
            raw_preferences = payload["preferences_snapshot"]
            if not isinstance(raw_preferences, dict) or raw_preferences.get("schema_version") != 3:
                raise LogicalAdvisorError("Expected schema-v3 preferences snapshot")
            preferences_snapshot = ProviderPreferences.from_payload(raw_preferences)
        continuation = payload.get("continuation", {})
        if not isinstance(continuation, dict) or (
            continuation and set(continuation) != {"session_id", "keep_chosen_model"}
        ):
            raise LogicalAdvisorError("Invalid continuation context")
        current_execution = payload.get("current_execution")
        if current_execution is not None and (
            not isinstance(current_execution, dict)
            or set(current_execution) != {"model_id", "reasoning_effort"}
            or any(
                value is not None and not isinstance(value, str)
                for value in current_execution.values()
            )
        ):
            raise LogicalAdvisorError("Invalid current execution context")
        raw_tags = payload.get("task_tags", [])
        if not isinstance(raw_tags, list):
            raise LogicalAdvisorError("Invalid task tags")
        return cls(
            current_execution=current_execution,
            continuation_session_id=continuation.get("session_id"),
            keep_chosen_model=continuation.get("keep_chosen_model", True),
            task_tags=tuple(raw_tags),
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
            schema_version=version,
            catalog_revision=payload.get("catalog_revision"),
            qualified_pool=qualified_pool,
            user_enabled_pool=user_enabled_pool,
            excluded_choices=excluded_choices,
            preferences_snapshot=preferences_snapshot,
        )


def freeze_logical_round(
    *,
    owner_id: str,
    host_id: str,
    round_id: str,
    settings_revision: str,
    catalog_revision: str,
    task: str,
    preferences: ProviderPreferences,
    catalog: tuple[LogicalChoice, ...],
    routes_by_choice: dict[str, tuple[QualifiedRoute, ...]],
    human_choice_id: str,
) -> LogicalFrozenRound:
    """Freeze the logical pool and advisor route before the single call."""
    pool, exclusions = selection_snapshot(preferences, catalog)
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
        schema_version=3,
        catalog_revision=catalog_revision,
        qualified_pool=tuple(sorted(catalog, key=lambda choice: choice.choice_id)),
        user_enabled_pool=pool,
        excluded_choices=exclusions,
        preferences_snapshot=preferences,
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
