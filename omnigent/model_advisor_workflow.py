"""Settings, frozen rounds and visible pre-run review for the model advisor.

Additive to model_advisor_core; no provider execution or runtime registration.
Catalogs passed here must be server-authorized. Browser requests contain IDs,
never trusted provider/account/entitlement metadata.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from collections.abc import Callable
from typing import Literal

from omnigent.model_advisor_core import (
    AdvisorContractError,
    Assignment,
    Candidate,
    PoolSnapshot,
    RoundChoices,
    build_advisor_request,
    parse_advisor_result,
    prepare_assignment,
    require_exact_available,
    task_fingerprint,
)


def _id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 256
        or value != value.strip()
        or any(ord(char) < 32 for char in value)
    ):
        raise AdvisorContractError("Invalid identifier")
    return value


def canonical_json(value: object) -> str:
    """Canonical JSON used for fingerprints and private durable snapshots."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def document_digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


@dataclass(frozen=True)
class AdvisorPreferences:
    """Server-persisted owner/host/profile preferences, not runtime credentials."""

    enabled: bool = False
    allowed_candidate_ids: tuple[str, ...] = ()
    advisor_candidate_id: str | None = None
    human_probability_percent: int = 50

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise AdvisorContractError("enabled must be a boolean")
        if type(self.human_probability_percent) is not int or not 0 <= self.human_probability_percent <= 100:
            raise AdvisorContractError("Human probability must be an integer from 0 to 100")
        if not isinstance(self.allowed_candidate_ids, tuple) or len(self.allowed_candidate_ids) > 128:
            raise AdvisorContractError("Invalid allowed candidate list")
        for candidate_id in self.allowed_candidate_ids:
            _id(candidate_id)
        if len(set(self.allowed_candidate_ids)) != len(self.allowed_candidate_ids):
            raise AdvisorContractError("Duplicate allowed candidate")
        if self.advisor_candidate_id is not None:
            _id(self.advisor_candidate_id)
        if self.enabled and (not self.allowed_candidate_ids or self.advisor_candidate_id is None):
            raise AdvisorContractError("Select allowed answers and an advisor before enabling")

    def to_payload(self) -> dict[str, object]:
        return {"schema_version": 1, **asdict(self), "allowed_candidate_ids": list(self.allowed_candidate_ids)}

    @classmethod
    def from_payload(cls, payload: object) -> AdvisorPreferences:
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version", "enabled", "allowed_candidate_ids",
            "advisor_candidate_id", "human_probability_percent",
        }:
            raise AdvisorContractError("Unexpected preferences shape")
        if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
            raise AdvisorContractError("Unsupported preferences version")
        if not isinstance(payload["allowed_candidate_ids"], list):
            raise AdvisorContractError("Allowed choices must be a list")
        return cls(
            enabled=payload["enabled"],
            allowed_candidate_ids=tuple(payload["allowed_candidate_ids"]),
            advisor_candidate_id=payload["advisor_candidate_id"],
            human_probability_percent=payload["human_probability_percent"],
        )


def stable_candidate_id(candidate: Candidate) -> str:
    """Host adapter helper: labels/catalog ordering cannot change selection IDs."""
    return "choice-" + document_digest(candidate.route_identity)


@dataclass(frozen=True)
class FrozenRound:
    """Initial task and human choice, locked BEFORE the advisor is called.

    This v1 keeps one initial task per round. Follow-ups retain its execution
    route. Extra attachments/context require a reviewed transport extension;
    they must not be silently omitted from the advisor's input.
    """

    owner_id: str
    host_id: str
    round_id: str
    settings_revision: str
    task: str
    pool: PoolSnapshot
    advisor: Candidate
    human_candidate_id: str
    human_probability_percent: int

    def __post_init__(self) -> None:
        for value in (self.owner_id, self.host_id, self.round_id, self.settings_revision):
            _id(value)
        task_fingerprint(self.task)
        if not isinstance(self.pool, PoolSnapshot) or not isinstance(self.advisor, Candidate):
            raise AdvisorContractError("Invalid round catalog")
        self.pool.get(self.human_candidate_id)
        if type(self.human_probability_percent) is not int or not 0 <= self.human_probability_percent <= 100:
            raise AdvisorContractError("Invalid assignment probability")

    @property
    def fingerprint(self) -> str:
        return document_digest({"policy_version": "visible-advisor-v1", **asdict(self)})

    def advisor_input(self) -> dict[str, object]:
        # Deliberately reuses the independently reviewed allowlist builder.
        return build_advisor_request(self.task, self.pool)

    def to_payload(self) -> dict[str, object]:
        return json.loads(canonical_json(asdict(self)))

    @classmethod
    def from_payload(cls, payload: dict) -> FrozenRound:
        """Decode only a server-written private snapshot, not a browser request."""
        data = dict(payload)
        pool = data["pool"]
        data["pool"] = PoolSnapshot(pool["catalog_revision"], tuple(Candidate(**row) for row in pool["candidates"]))
        data["advisor"] = Candidate(**data["advisor"])
        return cls(**data)


def freeze_round(
    *, owner_id: str, host_id: str, round_id: str, settings_revision: str,
    task: str, preferences: AdvisorPreferences, catalog: PoolSnapshot,
    human_candidate_id: str,
) -> FrozenRound:
    """Freeze the exact common pool; no automatic replacement of stale choices."""
    if not preferences.enabled:
        raise AdvisorContractError("Advisor is disabled")
    rows = tuple(catalog.get(key) for key in preferences.allowed_candidate_ids)
    pool = PoolSnapshot(catalog.catalog_revision, rows)
    advisor = catalog.get(preferences.advisor_candidate_id or "")
    return FrozenRound(
        owner_id, host_id, round_id, settings_revision, task, pool,
        advisor, human_candidate_id, preferences.human_probability_percent,
    )


@dataclass(frozen=True)
class ReviewDecision:
    """Visible recommendation and original assignment, prior to explicit Run.

    An override never rewrites assignment, probability or the original human
    pick. Such a round is marked overridden for as-treated comparison; retain
    original assignment for any later intent-to-treat analysis.
    """

    round_fingerprint: str
    human_candidate_id: str
    advisor_candidate_id: str
    rationale: str
    original_assignment: Assignment
    execution_candidate: Candidate
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
        return json.loads(canonical_json({**asdict(self), "comparison_group": self.comparison_group}))

    @classmethod
    def from_payload(cls, payload: dict) -> ReviewDecision:
        data = dict(payload)
        data.pop("comparison_group", None)
        assigned = dict(data["original_assignment"])
        assigned["selected"] = Candidate(**assigned["selected"])
        data["original_assignment"] = Assignment(**assigned)
        data["execution_candidate"] = Candidate(**data["execution_candidate"])
        return cls(**data)


def prepare_review(
    frozen: FrozenRound, raw_advice: str, *, randbelow: Callable[[int], int],
) -> ReviewDecision:
    """Call only within the durable store's claimed transaction; no network I/O."""
    advisor_pick, rationale = parse_advisor_result(raw_advice, frozen.pool)
    choices = RoundChoices(
        frozen.owner_id, frozen.host_id, frozen.round_id, frozen.settings_revision,
        task_fingerprint(frozen.task), frozen.pool, frozen.advisor,
        frozen.human_candidate_id, advisor_pick.candidate_id,
    )
    pct = frozen.human_probability_percent
    if pct == 50:
        assigned = prepare_assignment(choices, draw_bit=lambda: randbelow(2))
    elif frozen.human_candidate_id == advisor_pick.candidate_id:
        assigned = Assignment(choices.fingerprint, "same", advisor_pick, 1, 1)
    elif pct in (0, 100):
        arm = "human" if pct == 100 else "advisor"
        selected = frozen.pool.get(frozen.human_candidate_id) if pct == 100 else advisor_pick
        assigned = Assignment(choices.fingerprint, arm, selected, 1, 1)
    else:
        draw = randbelow(100)
        if type(draw) is not int or not 0 <= draw < 100:
            raise AdvisorContractError("Invalid assignment random draw")
        human = draw < pct
        assigned = Assignment(
            choices.fingerprint, "human" if human else "advisor",
            frozen.pool.get(frozen.human_candidate_id) if human else advisor_pick,
            pct if human else 100 - pct, 100,
        )
    return ReviewDecision(
        frozen.fingerprint, frozen.human_candidate_id, advisor_pick.candidate_id,
        rationale, assigned, assigned.selected, pct,
    )


def confirm_review(
    frozen: FrozenRound, review: ReviewDecision, live_catalog: PoolSnapshot,
    *, override_candidate_id: str | None = None, reason: str | None = None,
) -> ReviewDecision:
    """Explicit confirmation; availability failure cannot change the assignment."""
    if review.round_fingerprint != frozen.fingerprint:
        raise AdvisorContractError("Review belongs to another round")
    chosen = review.execution_candidate
    if override_candidate_id is not None:
        chosen = frozen.pool.get(override_candidate_id)
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
            raise AdvisorContractError("An override needs a short reason")
        review = replace(review, execution_candidate=chosen, overridden=True, override_reason=reason.strip())
    require_exact_available(replace(review.original_assignment, selected=chosen), live_catalog.candidates)
    return review
