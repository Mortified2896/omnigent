"""Owner-scoped orchestration for the concrete model advisor.

Glues the pure advisor contracts (``model_advisor_core`` /
``model_advisor_workflow``), the durable record store
(``model_advisor_repository``), the host transport (``host.advisor_call``)
and the session-creation path together:

- candidates come ONLY from the live host model catalog, classified by
  access lane — never from a browser assertion or a model-name heuristic;
- the caller's identity is the authenticated owner, never a request field;
- ``reserve_round(acquired=True)`` is the only permission for the single
  bounded advisor call, and ``confirm(acquired=True)`` for the one
  executor launch;
- public projections never include the private frozen round snapshot;
- a lost or crashed dispatch stays visibly uncertain instead of replaying.

This service deliberately contains no provider calls of its own: the
advisor model call runs on the host, and the executor launch goes through
the normal session-create path.
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from fastapi import HTTPException

from omnigent.entities.conversation import Conversation
from omnigent.model_advisor_core import AccessClass, AdvisorContractError, Candidate, PoolSnapshot
from omnigent.model_advisor_repository import AdvisorConflict, AdvisorRepository, Record
from omnigent.model_advisor_workflow import (
    AdvisorPreferences,
    FrozenRound,
    ReviewDecision,
    document_digest,
    freeze_round,
    stable_candidate_id,
)
from omnigent.server.host_registry import HostRegistry
from omnigent.stores.conversation_store import ADVISOR_ROUND_LABEL_KEY, ConversationStore
from omnigent.stores.host_store import HostStore

_logger = logging.getLogger("omnigent.server.model_advisor")

RESERVED_USER_LOCAL = "local"

_PROFILE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# Lanes whose rows may enter an advisor pool. A row's lane is the only
# accepted classification evidence: an OmniRoute gateway row qualifies only
# when it names a GLM route, and an OpenAI model name on a generic gateway
# is never proof of ChatGPT-plan access.
_LANE_CLASSIFICATIONS: dict[str, tuple[AccessClass, str, str]] = {
    # lane: (access_class, provider_id, connection_id)
    "codex-direct": ("chatgpt_plan", "openai-codex-subscription", "codex-login"),
    "glm-direct": ("glm_plan", "z.ai", "zai-direct"),
}
_OMNIROUTE_GLM: tuple[AccessClass, str, str] = ("glm_plan", "omniroute", "omniroute-glm")
_GLM_MODEL_PREFIXES = ("glm",)

ADVISOR_FRAME_TIMEOUT_S = 150.0

# Server-owned session labels for advisor-created sessions. The round-id key
# is the shared store constant the runner's exact-selection policy keys off;
# clients cannot forge any of them because the session is created server-side.
ADVISOR_ROUND_FINGERPRINT_LABEL_KEY = "omnigent.advisor.round_fingerprint"
ADVISOR_ROUND_GROUP_LABEL_KEY = "omnigent.advisor.comparison_group"


def _lane_classification(lane: str | None, model_id: str) -> tuple[AccessClass, str, str] | None:
    if lane in _LANE_CLASSIFICATIONS:
        # An extended catalog can carry GLM rows while probing Codex's
        # built-in subscription provider. Never turn a GLM checkpoint into a
        # ChatGPT-plan candidate merely because the probe stamped the direct
        # lane onto it.
        if lane == "codex-direct" and model_id.casefold().startswith(_GLM_MODEL_PREFIXES):
            return None
        return _LANE_CLASSIFICATIONS[lane]
    if lane == "omniroute" and model_id.casefold().startswith(_GLM_MODEL_PREFIXES):
        # An explicitly qualified OmniRoute GLM route stays a distinct GLM
        # option; every other gateway row is out of scope.
        return _OMNIROUTE_GLM
    return None


@dataclass(frozen=True)
class CatalogOption:
    """One projected concrete choice offered to the UI."""

    candidate_id: str
    model_id: str
    display_name: str
    lane_id: str
    reasoning_effort: str
    access_class: AccessClass
    is_default_effort: bool


@dataclass(frozen=True)
class HostCatalog:
    """The live qualified choice set for one host at one instant."""

    options: tuple[CatalogOption, ...]
    pool: PoolSnapshot
    candidates_by_id: dict[str, Candidate]

    @property
    def catalog_revision(self) -> str:
        return self.pool.catalog_revision


def _effort_names(row: dict[str, Any]) -> list[tuple[str, bool]]:
    """Extract (effort, is_default) pairs from one catalog row."""
    efforts: list[tuple[str, bool]] = []
    supported = row.get("supportedReasoningEfforts")
    default_effort = row.get("defaultReasoningEffort")
    if isinstance(supported, list):
        for entry in supported:
            name: object = None
            if isinstance(entry, dict):
                name = entry.get("reasoningEffort")
            elif isinstance(entry, str):
                name = entry
            if isinstance(name, str) and name.strip():
                efforts.append((name.strip(), name == default_effort))
    if efforts:
        return efforts
    if isinstance(default_effort, str) and default_effort.strip():
        return [(default_effort.strip(), True)]
    # Models without effort controls get an explicit, unambiguous marker.
    return [("not_applicable", False)]


def build_host_catalog(models: list[dict[str, Any]]) -> HostCatalog:
    """Map live host model-options rows into qualified advisor candidates.

    Rows without a recognized qualified lane are excluded, not downgraded.
    Later catalog rows never silently replace saved choices: candidate ids
    are content-addressed route identities.
    """
    options: list[CatalogOption] = []
    candidates: list[Candidate] = []
    for row in models:
        if not isinstance(row, dict):
            continue
        model_id = row.get("model") or row.get("id")
        lane = row.get("accessLane")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        classification = _lane_classification(lane if isinstance(lane, str) else None, model_id)
        if classification is None:
            continue
        access_class, provider_id, connection_id = classification
        display_name = row.get("displayName")
        if not isinstance(display_name, str) or not display_name:
            display_name = model_id
        for effort, is_default in _effort_names(row):
            candidate = Candidate(
                candidate_id="placeholder",
                lane_id=lane if isinstance(lane, str) else "",
                provider_id=provider_id,
                connection_id=connection_id,
                harness="codex",
                model_id=model_id.strip(),
                reasoning_effort=effort,
                access_class=access_class,
            )
            candidate = replace(candidate, candidate_id=stable_candidate_id(candidate))
            candidates.append(candidate)
            options.append(
                CatalogOption(
                    candidate_id=candidate.candidate_id,
                    model_id=candidate.model_id,
                    display_name=display_name,
                    lane_id=candidate.lane_id,
                    reasoning_effort=effort,
                    access_class=access_class,
                    is_default_effort=is_default,
                )
            )
    revision_source = sorted(candidate.route_identity for candidate in candidates)
    pool = PoolSnapshot(document_digest(revision_source), tuple(candidates))
    return HostCatalog(
        options=tuple(options),
        pool=pool,
        candidates_by_id={candidate.candidate_id: candidate for candidate in candidates},
    )


class ModelAdvisorService:
    """Authenticated round orchestration; one instance per server process."""

    def __init__(
        self,
        *,
        repository: AdvisorRepository,
        host_store: HostStore,
        host_registry: HostRegistry,
        conversation_store: ConversationStore,
        session_launcher: Callable[..., Any],
        randbelow: Callable[[int], int] = secrets.randbelow,
    ) -> None:
        self.repository = repository
        self._host_store = host_store
        self._host_registry = host_registry
        self._conversation_store = conversation_store
        self._session_launcher = session_launcher
        self._randbelow = randbelow
        self._background_tasks: set[asyncio.Task[None]] = set()

    # ── authorization helpers ────────────────────────────────

    @staticmethod
    def owner_id(user_id: str | None) -> str:
        return user_id if user_id is not None else RESERVED_USER_LOCAL

    @staticmethod
    def _validated_profile(profile: str) -> str:
        if not isinstance(profile, str) or not _PROFILE_PATTERN.match(profile):
            raise HTTPException(status_code=422, detail="Invalid advisor profile name")
        return profile

    def _authorized_host(self, user_id: str | None, host_id: str) -> Any:
        host = self._host_store.get_host(host_id)
        if host is None:
            raise HTTPException(status_code=404, detail="host not found")
        if user_id is not None and host.user_id != user_id:
            raise HTTPException(status_code=403, detail="not your host")
        return host

    def _live_host_connection(self, host: Any) -> Any:
        conn = self._host_registry.get(host.host_id)
        if conn is None:
            raise HTTPException(status_code=409, detail="host is offline")
        return conn

    # ── catalog ──────────────────────────────────────────────

    async def load_catalog(self, user_id: str | None, host_id: str) -> HostCatalog:
        """Fetch and qualify the host's live codex model options."""
        self._authorized_host(user_id, host_id)
        conn = self._live_host_connection(self._host_store.get_host(host_id))
        from omnigent.server.routes._host_model_options import request_host_model_options

        try:
            result = await request_host_model_options(
                host_registry=self._host_registry,
                host_conn=conn,
                harness="codex-native",
                timeout_s=60.0,
            )
        except ConnectionError as exc:
            raise HTTPException(status_code=502, detail="host connection lost") from exc
        except asyncio.TimeoutError as exc:
            raise HTTPException(
                status_code=504, detail="host model-options lookup timed out"
            ) from exc
        if result.get("status") != "ok":
            raise HTTPException(
                status_code=502,
                detail=str(result.get("error") or "host model-options lookup failed"),
            )
        models = result.get("models")
        rows = [row for row in models if isinstance(row, dict)] if isinstance(models, list) else []
        try:
            catalog = build_host_catalog(rows)
        except AdvisorContractError as exc:
            # An authenticated host can be online while no qualified direct
            # lane is currently available. Surface that state to the UI as a
            # visible catalog-unavailable response, never as a server 500.
            raise HTTPException(
                status_code=422,
                detail="No qualified concrete models are available for the advisor on this host",
            ) from exc
        if not catalog.options:
            raise HTTPException(
                status_code=422,
                detail="No qualified concrete models are available for the advisor on this host",
            )
        return catalog

    # ── preferences ──────────────────────────────────────────

    async def load_preferences(
        self, user_id: str | None, host_id: str, profile: str
    ) -> dict[str, Any] | None:
        owner = self.owner_id(user_id)
        self._validated_profile(profile)
        self._authorized_host(user_id, host_id)
        record = await asyncio.to_thread(self.repository.load_preferences, owner, host_id, profile)
        return None if record is None else self._preferences_projection(record)

    async def save_preferences(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        preferences: AdvisorPreferences,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._validated_profile(profile)
        self._authorized_host(user_id, host_id)
        # Saved ids must exist in the live qualified catalog: preferences
        # reference concrete routes, and stale ids are surfaced to the UI
        # with a reason, never silently rewritten.
        catalog = await self.load_catalog(user_id, host_id)
        for candidate_id in preferences.allowed_candidate_ids:
            if candidate_id not in catalog.candidates_by_id:
                raise HTTPException(
                    status_code=422,
                    detail=f"Allowed choice {candidate_id!r} is not a currently offered candidate",
                )
        if (
            preferences.advisor_candidate_id is not None
            and preferences.advisor_candidate_id not in catalog.candidates_by_id
        ):
            raise HTTPException(
                status_code=422,
                detail="Advisor choice is not a currently offered candidate",
            )
        try:
            record = await asyncio.to_thread(
                self.repository.save_preferences,
                owner,
                host_id,
                preferences,
                expected_version=expected_version,
                profile=profile,
            )
        except AdvisorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return self._preferences_projection(record)

    @staticmethod
    def _preferences_projection(record: Record) -> dict[str, Any]:
        payload = record.payload
        return {
            "object": "model_advisor.preferences",
            "version": record.version,
            "etag": record.etag,
            "state": record.state,
            "preferences": {
                "enabled": payload.get("enabled", False),
                "allowed_candidate_ids": list(payload.get("allowed_candidate_ids", ())),
                "advisor_candidate_id": payload.get("advisor_candidate_id"),
                "human_probability_percent": payload.get("human_probability_percent", 50),
            },
        }

    # ── rounds ───────────────────────────────────────────────

    async def _preferences_for_round(
        self,
        owner: str,
        host_id: str,
        profile: str,
        explicit: AdvisorPreferences | None = None,
    ) -> tuple[Record | None, AdvisorPreferences]:
        if explicit is not None:
            if not explicit.enabled:
                raise HTTPException(
                    status_code=422, detail="The model advisor is disabled in round settings"
                )
            return None, explicit
        record = await asyncio.to_thread(self.repository.load_preferences, owner, host_id, profile)
        if record is None:
            raise HTTPException(
                status_code=422,
                detail="No saved advisor settings for this host; save defaults first",
            )
        try:
            preferences = AdvisorPreferences.from_payload(record.payload)
        except AdvisorContractError as exc:
            raise HTTPException(
                status_code=422, detail=f"Saved advisor settings are invalid: {exc}"
            ) from exc
        if not preferences.enabled:
            raise HTTPException(
                status_code=422, detail="The model advisor is disabled in saved settings"
            )
        return record, preferences

    async def create_round(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        *,
        task: str,
        human_candidate_id: str,
        submission_key: str | None = None,
        preferences: AdvisorPreferences | None = None,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._validated_profile(profile)
        self._authorized_host(user_id, host_id)
        prefs_record, frozen_preferences = await self._preferences_for_round(
            owner, host_id, profile, explicit=preferences
        )
        if not isinstance(task, str) or not task.strip() or len(task) > 200_000:
            raise HTTPException(status_code=422, detail="A nonempty initial task is required")
        if human_candidate_id not in frozen_preferences.allowed_candidate_ids:
            raise HTTPException(
                status_code=422,
                detail="Your pick must be one of the allowed answers frozen for this round",
            )

        # The round id is deterministic for one submission identity. The
        # browser reuses submission_key across network retries; the server
        # still binds it to every meaningful input so a reused key cannot join
        # a different prompt/settings snapshot. An omitted key keeps older
        # clients idempotent by using the same content-derived identity.
        identity_key = submission_key or "implicit"
        submission_identity = document_digest(
            {
                "owner": owner,
                "host": host_id,
                "profile": profile,
                "task": task,
                "human_candidate_id": human_candidate_id,
                "preferences": frozen_preferences.to_payload(),
                "submission_key": identity_key,
            }
        )
        round_id = "adviseround-" + submission_identity

        # Avoid a second catalog request or background call on an exact retry.
        # The durable owner/host scope is checked by the repository itself.
        existing = await asyncio.to_thread(self.repository.load_round, owner, host_id, round_id)
        if existing is not None:
            return await self.load_round(user_id, host_id, round_id)

        catalog = await self.load_catalog(user_id, host_id)
        settings_revision = (
            f"prefs-v{prefs_record.version}-{prefs_record.etag}"
            if prefs_record is not None
            else "round-draft-" + document_digest(frozen_preferences.to_payload())
        )
        try:
            frozen = freeze_round(
                owner_id=owner,
                host_id=host_id,
                round_id=round_id,
                settings_revision=settings_revision,
                task=task,
                preferences=frozen_preferences,
                catalog=catalog.pool,
                human_candidate_id=human_candidate_id,
            )
        except AdvisorContractError as exc:
            # Includes stale saved choices that left the live catalog: the
            # round is rejected explicitly, never silently re-chosen.
            raise HTTPException(
                status_code=422,
                detail=f"Advisor settings no longer match the live catalog: {exc}",
            ) from exc
        try:
            claim = await asyncio.to_thread(
                self.repository.reserve_round, frozen, submission_key=submission_key
            )
        except AdvisorConflict as exc:
            # Two identical retries can race between the read-above and the
            # insert. Once the first transaction commits, return its durable
            # reservation rather than making the browser see a false conflict
            # or starting a second advisor call.
            existing = await asyncio.to_thread(
                self.repository.load_round, owner, host_id, round_id
            )
            if existing is not None:
                return await self.load_round(user_id, host_id, round_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claim.acquired:
            self._schedule_advisor_call(owner, host_id, round_id, frozen)
        return await self.load_round(user_id, host_id, round_id)

    def _schedule_advisor_call(
        self, owner: str, host_id: str, round_id: str, frozen: FrozenRound
    ) -> None:
        """Start the one permitted advisor call in the background."""

        async def _run() -> None:
            request = frozen.advisor_input()
            advisor = frozen.advisor
            try:
                from omnigent.server.routes._advisor_call import request_host_advisor_call

                conn = self._live_host_connection(self._host_store.get_host(host_id))
                result = await request_host_advisor_call(
                    host_registry=self._host_registry,
                    host_conn=conn,
                    request=request,
                    model=advisor.model_id,
                    access_lane=advisor.lane_id,
                    reasoning_effort=(
                        None
                        if advisor.reasoning_effort == "not_applicable"
                        else advisor.reasoning_effort
                    ),
                    timeout_s=ADVISOR_FRAME_TIMEOUT_S,
                )
            except (HTTPException, ConnectionError, asyncio.TimeoutError) as exc:
                self._finish_advisor_failure(owner, host_id, round_id, f"advisor transport: {exc}")
                return
            if result.get("status") != "ok" or not isinstance(result.get("raw_output"), str):
                reason = str(result.get("error") or "advisor call failed on the host")
                self._finish_advisor_failure(owner, host_id, round_id, reason)
                return
            try:
                await asyncio.to_thread(
                    self.repository.finish_advice,
                    owner,
                    host_id,
                    round_id,
                    str(result["raw_output"]),
                    randbelow=self._randbelow,
                    overhead=self._advisor_overhead(result),
                )
            except AdvisorConflict:
                # Cancelled or already finished while the call was in flight;
                # the stored state stays authoritative either way.
                return
            except AdvisorContractError as exc:
                self._finish_advisor_failure(
                    owner, host_id, round_id, f"advisor output rejected: {exc}"
                )
                return

        def _log_crash(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                _logger.exception("advisor background call crashed", exc_info=task.exception())
            self._background_tasks.discard(task)

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(_log_crash)

    def _finish_advisor_failure(
        self, owner: str, host_id: str, round_id: str, reason: str
    ) -> None:
        try:
            self.repository.mark_round_failed(owner, host_id, round_id, reason=reason[:600])
        except (AdvisorConflict, AdvisorContractError):
            _logger.warning("Could not mark advisor round %s failed: %s", round_id, reason)
        _logger.warning("Advisor round %s blocked: %s", round_id, reason)

    @staticmethod
    def _advisor_overhead(result: dict[str, Any]) -> dict[str, Any]:
        """Project bounded provider telemetry into the finish transaction."""
        return {
            "latency_ms": result.get("latency_ms"),
            "input_tokens": result.get("input_tokens"),
            "output_tokens": result.get("output_tokens"),
            "cached_input_tokens": result.get("cached_input_tokens"),
            "response_id": result.get("response_id"),
        }

    async def load_round(self, user_id: str | None, host_id: str, round_id: str) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._authorized_host(user_id, host_id)
        record = await asyncio.to_thread(self.repository.load_round, owner, host_id, round_id)
        if record is None:
            raise HTTPException(status_code=404, detail="advisor round not found")
        projection = self._round_projection(record, round_id)
        session_id = projection.get("execution", {}).get("session_id")
        if isinstance(session_id, str) and session_id:
            requested = await asyncio.to_thread(self.requested_execution, session_id)
            if requested is not None:
                projection["requested_execution"] = requested
            actual = record.payload.get("actual_execution")
            if not isinstance(actual, dict):
                actual = await asyncio.to_thread(self.actual_execution, session_id)
            projection["actual_execution"] = actual or {
                "status": "unknown",
                "reason": "This runtime did not provide verified provider execution telemetry",
            }
        return projection

    @staticmethod
    def _round_projection(record: Record, round_id: str) -> dict[str, Any]:
        payload = record.payload
        projection: dict[str, Any] = {
            "object": "model_advisor.round",
            "round_id": round_id,
            "state": record.state,
            "version": record.version,
            "etag": record.etag,
            "failure_reason": payload.get("failure_reason"),
        }
        review = payload.get("review")
        if isinstance(review, dict):
            decision = ReviewDecision.from_payload(review)
            projection["review"] = {
                "round_fingerprint": decision.round_fingerprint,
                "human_candidate_id": decision.human_candidate_id,
                "advisor_candidate_id": decision.advisor_candidate_id,
                "rationale": decision.rationale,
                "assigned_candidate_id": decision.original_assignment.selected.candidate_id,
                "assigned_arm": decision.original_assignment.arm,
                "human_probability_percent": decision.human_probability_percent,
                "overridden": decision.overridden,
                "override_reason": decision.override_reason,
                "comparison_group": decision.comparison_group,
            }
        session_id = payload.get("execution_session_id")
        projection["execution"] = {
            "session_id": session_id,
            # dispatch_claimed without a binding is an explicitly uncertain
            # dispatch: a crash happened between claim and session creation.
            "uncertain": record.state == "dispatch_claimed",
        }
        overhead = payload.get("advisor_overhead")
        if isinstance(overhead, dict):
            projection["advisor_overhead"] = overhead
        return projection

    async def confirm_round(
        self,
        user_id: str | None,
        host_id: str,
        round_id: str,
        *,
        expected_version: int,
        override_candidate_id: str | None = None,
        reason: str | None = None,
        launch: dict[str, Any],
        request: Any = None,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._authorized_host(user_id, host_id)
        catalog = await self.load_catalog(user_id, host_id)
        try:
            claim = await asyncio.to_thread(
                self.repository.confirm,
                owner,
                host_id,
                round_id,
                expected_version=expected_version,
                live_catalog=catalog.pool,
                override_candidate_id=override_candidate_id,
                reason=reason,
            )
        except AdvisorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except AdvisorContractError as exc:
            # Includes the exact-availability guard: the assigned route
            # disappeared and the round will NOT be re-pointed at anything.
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not claim.acquired:
            return await self.load_round(user_id, host_id, round_id)
        try:
            session_id = await self._launch_executor(
                user_id, host_id, claim.record, catalog, launch, request
            )
        except Exception as exc:
            _logger.exception("Advisor executor launch failed for round %s", round_id)
            projection = self._round_projection(claim.record, round_id)
            projection["launch_error"] = str(exc)
            return projection
        try:
            await asyncio.to_thread(
                self.repository.bind_dispatch, owner, host_id, round_id, session_id=session_id
            )
        except AdvisorConflict as exc:
            _logger.error("Advisor session binding failed for round %s: %s", round_id, exc)
        return await self.load_round(user_id, host_id, round_id)

    async def _launch_executor(
        self,
        user_id: str | None,
        host_id: str,
        record: Record,
        catalog: HostCatalog,
        launch: dict[str, Any],
        request: Any,
    ) -> str:
        """Create the ONE session for the claimed round via the normal path."""
        from omnigent.server.schemas import SessionCreateRequest, SessionEventInput

        payload = record.payload
        frozen = FrozenRound.from_payload(payload["frozen"])
        review = ReviewDecision.from_payload(payload["review"])
        candidate = review.execution_candidate
        if candidate.candidate_id not in catalog.candidates_by_id:
            raise RuntimeError("Assigned candidate left the live catalog before launch")
        if not isinstance(launch.get("agent_id"), str) or not launch.get("agent_id"):
            raise RuntimeError("An agent_id is required to launch the round")
        workspace = launch.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            raise RuntimeError("A workspace is required to launch the round")
        labels = {
            ADVISOR_ROUND_LABEL_KEY: frozen.round_id,
            ADVISOR_ROUND_FINGERPRINT_LABEL_KEY: frozen.fingerprint,
            ADVISOR_ROUND_GROUP_LABEL_KEY: review.comparison_group,
        }
        if candidate.lane_id:
            labels["omnigent.access_lane"] = candidate.lane_id
        body = SessionCreateRequest(
            agent_id=launch["agent_id"],
            initial_items=[
                SessionEventInput(
                    type="message",
                    data={
                        "role": "user",
                        "content": [{"type": "input_text", "text": frozen.task}],
                    },
                )
            ],
            title=review.comparison_group,
            labels=labels,
            host_id=host_id,
            workspace=workspace,
            model_override=candidate.model_id,
            reasoning_effort=(
                None
                if candidate.reasoning_effort == "not_applicable"
                else candidate.reasoning_effort
            ),
            terminal_launch_args=launch.get("terminal_launch_args"),
        )
        session = await self._session_launcher(
            body,
            user_id=user_id,
            request=request,
        )
        return session.id

    async def cancel_round(
        self,
        user_id: str | None,
        host_id: str,
        round_id: str,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._authorized_host(user_id, host_id)
        try:
            await asyncio.to_thread(
                self.repository.cancel,
                owner,
                host_id,
                round_id,
                expected_version=expected_version,
            )
        except AdvisorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await self.load_round(user_id, host_id, round_id)

    # ── live execution inspection ────────────────────────────

    def requested_execution(self, session_id: str) -> dict[str, Any] | None:
        """Return requested session metadata, never relabeled as observed.

        The conversation row contains the launch request. Provider/runtime
        evidence is a separate concern and is reported as unknown until the
        runtime writes an actual execution record.
        """
        conv: Conversation | None = self._conversation_store.get_conversation(session_id)
        if conv is None:
            return None
        labels = conv.labels or {}
        return {
            "session_id": session_id,
            "host_id": conv.host_id,
            "model": conv.model_override,
            "reasoning_effort": getattr(conv, "reasoning_effort", None),
            "access_lane": labels.get("omnigent.access_lane"),
            "comparison_group": labels.get(ADVISOR_ROUND_GROUP_LABEL_KEY),
        }

    def actual_execution(self, session_id: str) -> dict[str, Any] | None:
        """Return only harness-reported execution evidence for one session."""
        conv: Conversation | None = self._conversation_store.get_conversation(session_id)
        if conv is None or not isinstance(conv.reported_model, str) or not conv.reported_model:
            return None
        return {
            "status": "observed",
            "model": conv.reported_model,
            # This runtime has no separate observed effort/lane event. Do not
            # copy requested metadata into these fields.
            "reasoning_effort": None,
            "access_lane": None,
        }
