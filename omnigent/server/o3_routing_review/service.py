"""Application service coordinating adviser, deterministic evaluation, and audit."""

from __future__ import annotations

import hashlib
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone

from omnigent.process_logging import env_truthy

from .adviser import OmniRouteRoutingAdviser, RoutingAdviser
from .evaluator import evaluate_candidates, hard_capable_candidates
from .models import (
    AdviserAnalysis,
    ApprovedConstraints,
    BenchmarkRequirement,
    CandidateEvaluation,
    CandidateSnapshot,
    CandidateStatus,
    CleanupResult,
    DecisionAction,
    DecompositionItem,
    Disposition,
    ProposalAdjustmentRequest,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalOutcomeRequest,
    ProposalSessionLinkRequest,
    RoutingProposal,
)
from .omniroute import OmniRouteClient, OmniRouteError
from .registry import BenchmarkRegistry
from .store import ProposalStore

O3_ROUTING_REVIEW_ENV = "OMNIGENT_O3_ROUTING_REVIEW"
PROPOSAL_TTL_ENV = "OMNIGENT_O3_ROUTING_REVIEW_TTL_SECONDS"
DEFAULT_PROPOSAL_TTL_SECONDS = 24 * 60 * 60

_logger = logging.getLogger(__name__)


def o3_routing_review_enabled() -> bool:
    return env_truthy(os.environ.get(O3_ROUTING_REVIEW_ENV))


class RoutingReviewError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        status_code: int = 400,
        code: str = "invalid_request",
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


class O3RoutingReviewService:
    def __init__(
        self,
        *,
        registry: BenchmarkRegistry,
        omniroute: OmniRouteClient,
        adviser: RoutingAdviser,
        store: ProposalStore,
        ttl_seconds: int = DEFAULT_PROPOSAL_TTL_SECONDS,
    ) -> None:
        if ttl_seconds < 60 or ttl_seconds > 30 * 24 * 60 * 60:
            raise ValueError("O3 proposal TTL must be between 60 seconds and 30 days")
        self.registry = registry
        self.omniroute = omniroute
        self.adviser = adviser
        self.store = store
        self.ttl_seconds = ttl_seconds

    @classmethod
    def from_env(cls) -> O3RoutingReviewService:
        client = OmniRouteClient.from_env()
        raw_ttl = os.environ.get(PROPOSAL_TTL_ENV)
        ttl = int(raw_ttl) if raw_ttl is not None else DEFAULT_PROPOSAL_TTL_SECONDS
        registry = BenchmarkRegistry()
        return cls(
            registry=registry,
            omniroute=client,
            adviser=OmniRouteRoutingAdviser(client),
            store=ProposalStore(),
            ttl_seconds=ttl,
        )

    def _require(self, proposal_id: str) -> RoutingProposal:
        proposal = self.store.get(proposal_id)
        if proposal is None:
            raise RoutingReviewError(
                "routing proposal not found",
                status_code=404,
                code="not_found",
            )
        return proposal

    def get_proposal(self, proposal_id: str) -> RoutingProposal:
        """Return a persisted proposal or raise the public not-found error."""
        return self._require(proposal_id)

    def _validate_analysis(self, analysis: AdviserAnalysis) -> None:
        if len(analysis.benchmark_requirements) != 1:
            raise RoutingReviewError(
                "the O3 MVP adviser must return exactly one benchmark requirement",
                status_code=502,
                code="invalid_adviser_output",
            )
        for requirement in analysis.benchmark_requirements:
            try:
                self.registry.require_slice(requirement)
            except ValueError as exc:
                raise RoutingReviewError(
                    str(exc), status_code=502, code="invalid_adviser_output"
                ) from exc

    async def create_proposal(self, request: ProposalCreateRequest) -> RoutingProposal:
        prompt = request.prompt
        if not prompt.strip():
            raise RoutingReviewError("prompt must not be blank")
        await self.cleanup_expired()
        candidates = await self.omniroute.live_candidates(self.registry.candidates)
        analysis = await self.adviser.analyse(
            prompt=prompt,
            workspace_summary=request.workspace_summary,
            registry=self.registry,
            candidates=candidates,
        )
        self._validate_analysis(analysis)
        constraints = ApprovedConstraints(
            benchmark=analysis.benchmark_requirements[0],
            reasoning_effort=analysis.proposed_reasoning_effort,
            risk=analysis.risk,
            evidence_policy=analysis.evidence_policy,
            cost_quota_preference="preserve_subscription",
        )
        result = evaluate_candidates(self.registry, analysis, candidates, constraints)
        final_disposition = result.disposition
        if result.disposition is Disposition.DECOMPOSE:
            gap = result.frontier.capability_gap or "No adequate route."
            decomposed = await self.adviser.decompose(
                prompt=prompt,
                workspace_summary=request.workspace_summary,
                registry=self.registry,
                candidates=candidates,
                prior=analysis,
                capability_gap=gap,
            )
            self._validate_analysis(decomposed)
            decomposition = self._validate_decomposition(decomposed, candidates)
            analysis = analysis.model_copy(
                update={
                    "decomposition": decomposition,
                    "rationale": decomposed.rationale,
                    "disposition": (Disposition.DECOMPOSE if decomposition else Disposition.DEFER),
                }
            )
            final_disposition = analysis.disposition
        else:
            analysis = analysis.model_copy(update={"disposition": result.disposition})

        now = datetime.now(timezone.utc)
        proposal = RoutingProposal(
            proposal_id=str(uuid.uuid4()),
            created_at=now,
            updated_at=now,
            expires_at=now + timedelta(seconds=self.ttl_seconds),
            prompt_fingerprint="sha256:" + hashlib.sha256(prompt.encode()).hexdigest(),
            workspace_summary=request.workspace_summary,
            adviser=analysis,
            approved_constraints=constraints,
            evaluations=result.evaluations,
            frontier=result.frontier,
            disposition=final_disposition,
        )
        self.store.put(proposal)
        return proposal

    def _validate_decomposition(
        self,
        analysis: AdviserAnalysis,
        candidates: list[CandidateSnapshot],
    ) -> list[DecompositionItem]:
        validated: list[DecompositionItem] = []
        seen_orders: set[int] = set()
        for item in sorted(analysis.decomposition, key=lambda value: value.dependency_order):
            if item.dependency_order in seen_orders:
                raise RoutingReviewError(
                    "decomposition dependency_order values must be unique",
                    status_code=502,
                    code="invalid_adviser_output",
                )
            seen_orders.add(item.dependency_order)
            requirement = BenchmarkRequirement(
                benchmark_id=item.benchmark_id,
                version=item.version,
                slice_id=item.slice_id,
                minimum_score=item.minimum_score,
                reason=item.competence_reduction,
            )
            try:
                self.registry.require_slice(requirement)
            except ValueError as exc:
                raise RoutingReviewError(
                    str(exc), status_code=502, code="invalid_adviser_output"
                ) from exc
            sub_analysis = analysis.model_copy(
                update={
                    "benchmark_requirements": [requirement],
                    "proposed_reasoning_effort": item.reasoning_effort,
                    "risk": item.risk,
                }
            )
            constraints = ApprovedConstraints(
                benchmark=requirement,
                reasoning_effort=item.reasoning_effort,
                risk=item.risk,
                evidence_policy=analysis.evidence_policy,
                cost_quota_preference="preserve_subscription",
            )
            evaluated = evaluate_candidates(self.registry, sub_analysis, candidates, constraints)
            passing = [
                value.candidate.candidate_id
                for value in evaluated.evaluations
                if value.status in {CandidateStatus.PASS, CandidateStatus.PROVISIONAL}
            ]
            validated.append(
                item.model_copy(
                    update={
                        "passing_candidates": passing,
                        "blocked": item.blocked or not passing,
                    }
                )
            )
        return validated

    async def adjust_proposal(
        self, proposal_id: str, request: ProposalAdjustmentRequest
    ) -> RoutingProposal:
        proposal = self._require(proposal_id)
        if proposal.decision is not None:
            raise RoutingReviewError("a decided proposal cannot be adjusted", status_code=409)
        current = proposal.approved_constraints
        benchmark = current.benchmark.model_copy(
            update={
                key: value
                for key, value in {
                    "benchmark_id": request.benchmark_id,
                    "version": request.version,
                    "slice_id": request.slice_id,
                    "minimum_score": request.minimum_score,
                }.items()
                if value is not None
            }
        )
        try:
            self.registry.require_slice(benchmark)
        except ValueError as exc:
            raise RoutingReviewError(str(exc)) from exc
        constraints = current.model_copy(
            update={
                "benchmark": benchmark,
                **(
                    {"reasoning_effort": request.reasoning_effort}
                    if request.reasoning_effort
                    else {}
                ),
                **({"risk": request.risk} if request.risk else {}),
                **(
                    {"evidence_policy": request.evidence_policy} if request.evidence_policy else {}
                ),
                **(
                    {"cost_quota_preference": request.cost_quota_preference}
                    if request.cost_quota_preference
                    else {}
                ),
            }
        )
        candidates = await self.omniroute.live_candidates(self.registry.candidates)
        analysis = proposal.adviser.model_copy(
            update={
                "benchmark_requirements": [benchmark],
                "proposed_reasoning_effort": constraints.reasoning_effort,
                "risk": constraints.risk,
                "evidence_policy": constraints.evidence_policy,
                "decomposition": [],
            }
        )
        result = evaluate_candidates(self.registry, analysis, candidates, constraints)
        analysis = analysis.model_copy(update={"disposition": result.disposition})
        updated = proposal.model_copy(
            update={
                "updated_at": datetime.now(timezone.utc),
                "adviser": analysis,
                "approved_constraints": constraints,
                "evaluations": result.evaluations,
                "frontier": result.frontier,
                "disposition": result.disposition,
            }
        )
        self.store.put(updated)
        return updated

    async def decide_proposal(
        self, proposal_id: str, request: ProposalDecisionRequest
    ) -> RoutingProposal:
        proposal = self._require(proposal_id)
        if proposal.decision is not None:
            if proposal.decision is request.action:
                return proposal
            raise RoutingReviewError("proposal already has a different decision", status_code=409)
        if request.action in {DecisionAction.DECLINE, DecisionAction.DEFER}:
            updated = proposal.model_copy(
                update={
                    "decision": request.action,
                    "decision_reason": request.reason,
                    "updated_at": datetime.now(timezone.utc),
                    "terminal_disposition": request.action.value,
                }
            )
            self.store.put(updated)
            return updated

        selected: list[CandidateEvaluation]
        decision_reason = request.reason
        if request.action is DecisionAction.RUN_ANYWAY:
            if not request.confirm_run_anyway:
                raise RoutingReviewError(
                    "run anyway requires explicit confirmation",
                    status_code=409,
                )
            if request.reason is None or len(request.reason.strip()) < 8:
                raise RoutingReviewError(
                    "run anyway requires a persisted reason of at least 8 characters"
                )
            selected = hard_capable_candidates(proposal.evaluations)
        else:
            selected = [
                item
                for item in proposal.evaluations
                if item.status in {CandidateStatus.PASS, CandidateStatus.PROVISIONAL}
            ]
            if any(item.status is CandidateStatus.PROVISIONAL for item in selected) and not (
                request.acknowledge_provisional
            ):
                raise RoutingReviewError(
                    "provisional evidence requires deliberate acknowledgement", status_code=409
                )
        if not selected:
            raise RoutingReviewError(
                "no structurally usable candidate exists for the approved effort",
                status_code=409,
                code="no_adequate_route",
            )
        try:
            combo_name, combo_definition = await self.omniroute.create_derived_combo(
                proposal.proposal_id,
                selected,
                reasoning_effort=proposal.approved_constraints.reasoning_effort,
            )
        except OmniRouteError as exc:
            raise RoutingReviewError(
                str(exc),
                status_code=502,
                code="combo_lifecycle_error",
            ) from exc
        updated = proposal.model_copy(
            update={
                "decision": request.action,
                "decision_reason": decision_reason,
                "derived_combo_name": combo_name,
                "derived_combo_definition": combo_definition,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.store.put(updated)
        return updated

    def link_session(
        self, proposal_id: str, request: ProposalSessionLinkRequest
    ) -> RoutingProposal:
        proposal = self._require(proposal_id)
        if proposal.decision not in {DecisionAction.APPROVE, DecisionAction.RUN_ANYWAY}:
            raise RoutingReviewError(
                "only an approved proposal can link a session",
                status_code=409,
            )
        if proposal.derived_combo_name is None:
            raise RoutingReviewError("approved proposal has no derived Combo", status_code=409)
        if proposal.session_id is not None and proposal.session_id != request.session_id:
            raise RoutingReviewError(
                "proposal is already linked to another session",
                status_code=409,
            )
        updated = proposal.model_copy(
            update={"session_id": request.session_id, "updated_at": datetime.now(timezone.utc)}
        )
        self.store.put(updated)
        return updated

    def record_outcome(self, proposal_id: str, request: ProposalOutcomeRequest) -> RoutingProposal:
        proposal = self._require(proposal_id)
        actual_fields = {
            key: value
            for key, value in {
                "actual_provider": request.actual_provider,
                "actual_model": request.actual_model,
                "actual_reasoning_effort": request.actual_reasoning_effort,
            }.items()
            if value is not None
        }
        updated = proposal.model_copy(
            update={
                "task_outcome": request.outcome,
                "terminal_disposition": request.terminal_disposition,
                **actual_fields,
                "updated_at": datetime.now(timezone.utc),
            }
        )
        self.store.put(updated)
        return updated

    async def sync_execution_provenance(
        self,
        session_id: str,
        *,
        external_status: str,
    ) -> list[RoutingProposal]:
        """Reconcile sanitized OmniRoute call logs after a terminal Codex turn edge."""
        if external_status not in {"idle", "failed"}:
            raise ValueError("execution provenance sync requires idle or failed status")
        proposals = [item for item in self.store.list() if item.session_id == session_id]
        updated_proposals: list[RoutingProposal] = []
        for proposal in proposals:
            combo_name = proposal.derived_combo_name
            if combo_name is None:
                continue
            observed = await self.omniroute.execution_provenance_for_session(
                derived_combo_name=combo_name,
                proposal_created_at=proposal.created_at,
            )
            if not observed and external_status == "idle":
                continue
            by_id = {item.call_log_id: item for item in proposal.execution_provenance}
            by_id.update({item.call_log_id: item for item in observed})
            provenance = sorted(
                by_id.values(),
                key=lambda item: (item.timestamp, item.call_log_id),
            )
            latest = provenance[-1] if provenance else None
            latest_effort = next(
                (
                    item.reasoning_effort
                    for item in reversed(provenance)
                    if item.reasoning_effort is not None
                ),
                None,
            )
            now = datetime.now(timezone.utc)
            automatic_outcome = "completed" if external_status == "idle" else "failed"
            update: dict[str, object] = {
                "execution_provenance": provenance,
                "execution_status": external_status,
                "provenance_synced_at": now,
                "updated_at": now,
            }
            if proposal.task_outcome is None or proposal.task_outcome in {"completed", "failed"}:
                update["task_outcome"] = automatic_outcome
            if proposal.terminal_disposition is None or proposal.terminal_disposition in {
                "completed",
                "failed",
            }:
                update["terminal_disposition"] = automatic_outcome
            if latest is not None:
                update.update(
                    {
                        "actual_provider": latest.provider,
                        "actual_model": latest.model,
                        "actual_reasoning_effort": latest_effort,
                    }
                )
            updated = proposal.model_copy(update=update)
            self.store.put(updated)
            updated_proposals.append(updated)
        return updated_proposals

    async def cleanup_expired(self, *, now: datetime | None = None) -> CleanupResult:
        current = now or datetime.now(timezone.utc)
        proposals = self.store.list()
        result = CleanupResult(inspected=len(proposals))
        for proposal in proposals:
            name = proposal.derived_combo_name
            if name is None or proposal.expires_at > current:
                continue
            if (
                proposal.session_id is not None
                and proposal.terminal_disposition != "session_deleted"
            ):
                result.retained_active.append(name)
                continue
            try:
                await self.omniroute.delete_derived_combo(name)
                result.removed_combos.append(name)
                updated = proposal.model_copy(
                    update={
                        "terminal_disposition": proposal.terminal_disposition or "ttl_cleaned",
                        "updated_at": current,
                    }
                )
                self.store.put(updated)
            except OmniRouteError as exc:
                result.failures[name] = str(exc)
        return result

    async def cleanup_session(self, session_id: str) -> CleanupResult:
        proposals = [item for item in self.store.list() if item.session_id == session_id]
        result = CleanupResult(inspected=len(proposals))
        for proposal in proposals:
            name = proposal.derived_combo_name
            if name is None:
                continue
            try:
                await self.omniroute.delete_derived_combo(name)
                result.removed_combos.append(name)
                self.store.put(
                    proposal.model_copy(
                        update={
                            "terminal_disposition": "session_deleted",
                            "updated_at": datetime.now(timezone.utc),
                        }
                    )
                )
            except OmniRouteError as exc:
                result.failures[name] = str(exc)
        return result


_default_service: O3RoutingReviewService | None = None


def get_o3_routing_review_service() -> O3RoutingReviewService:
    global _default_service
    if _default_service is None:
        _default_service = O3RoutingReviewService.from_env()
    return _default_service


def reset_o3_routing_review_service_for_tests() -> None:
    global _default_service
    _default_service = None


async def cleanup_o3_session_best_effort(session_id: str) -> None:
    """Delete an owned derived Combo after session deletion, never blocking delete."""
    if not o3_routing_review_enabled():
        return
    try:
        result = await get_o3_routing_review_service().cleanup_session(session_id)
        if result.failures:
            _logger.warning(
                "O3 derived Combo cleanup deferred for %s: %s",
                session_id,
                result.failures,
            )
    except Exception:  # noqa: BLE001 — session deletion must remain available
        _logger.warning("O3 derived Combo cleanup failed for %s", session_id, exc_info=True)


async def sync_o3_execution_best_effort(session_id: str, external_status: str) -> None:
    """Capture O3 execution metadata without making session status delivery fragile."""
    if not o3_routing_review_enabled() or external_status not in {"idle", "failed"}:
        return
    try:
        await get_o3_routing_review_service().sync_execution_provenance(
            session_id,
            external_status=external_status,
        )
    except Exception:  # noqa: BLE001 — provenance sync must not break the harness
        _logger.warning(
            "O3 execution provenance sync failed for %s",
            session_id,
            exc_info=True,
        )
