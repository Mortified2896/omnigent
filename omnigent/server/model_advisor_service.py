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
import json
import logging
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from fastapi import HTTPException

from omnigent.entities.conversation import Conversation
from omnigent.model_advisor_core import AccessClass, AdvisorContractError, Candidate, PoolSnapshot
from omnigent.model_advisor_provider_policy import (
    LogicalChoice,
    ProviderPolicyError,
    ProviderPreferences,
    QualifiedRoute,
    TransportPlan,
    migrate_v1,
    permitted_fallback,
    plan_transport,
)
from omnigent.model_advisor_provider_workflow import (
    LogicalAdvisorError,
    LogicalFrozenRound,
    LogicalReviewDecision,
    freeze_logical_round,
)
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
from omnigent.stores.conversation_store import (
    ADVISOR_CONNECTION_LABEL_KEY,
    ADVISOR_DISPATCH_ROUTE_LABEL_KEY,
    ADVISOR_LOGICAL_CHOICE_LABEL_KEY,
    ADVISOR_ROUND_LABEL_KEY,
    ADVISOR_TRANSPORT_PLAN_LABEL_KEY,
    ConversationStore,
)
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

# The namespace and alias pairs are provider vocabulary, not a prefix-based
# provider classifier.  They are derived from the live Codex and OmniRoute
# catalogs on the RTX host.  A new gateway spelling must be added only after
# both sides have been qualified by their lane probes.
_OPENAI_OMNIROUTE_TO_CANONICAL: dict[str, str] = {
    "codex/gpt-6-astra": "gpt-6-astra",
    "codex/gpt-5.6-sol": "gpt-5.6-sol",
    "codex/gpt-5.6-terra": "gpt-5.6-terra",
    "codex/gpt-5.6-luna": "gpt-5.6-luna",
    "codex/gpt-5.5": "gpt-5.5",
}
# The Codex direct catalog can use the same provider-prefixed spelling as its
# OmniRoute catalog. These aliases were qualified on both live lanes; retain
# an explicit direct-lane map so the catalog spelling does not become a second
# logical checkpoint. Do not strip ``codex/`` generically: the prefix alone is
# not proof that two provider rows are equivalent.
_OPENAI_DIRECT_TO_CANONICAL: dict[str, str] = {
    "codex/gpt-6-astra": "gpt-6-astra",
    "codex/gpt-5.6-luna": "gpt-5.6-luna",
}
from omnigent.models.glm_model_vocabulary import (  # noqa: E402
    GLM_DIRECT_MODELS,
    GLM_OMNIROUTE_ROUTES,
    glm_display_name,
)

ADVISOR_FRAME_TIMEOUT_S = 150.0

# Server-owned session labels for advisor-created sessions. The round-id key
# is the shared store constant the runner's exact-selection policy keys off;
# clients cannot forge any of them because the session is created server-side.
ADVISOR_ROUND_FINGERPRINT_LABEL_KEY = "omnigent.advisor.round_fingerprint"
ADVISOR_ROUND_GROUP_LABEL_KEY = "omnigent.advisor.comparison_group"


class PreDispatchRouteFailure(RuntimeError):
    """Typed host/runner evidence for a safe pre-upstream route retry."""

    def __init__(
        self,
        cause: str,
        *,
        upstream_not_started: bool,
        output_seen: bool = False,
        tools_started: bool = False,
        thread_bound: bool = False,
    ) -> None:
        super().__init__(cause)
        self.cause = cause
        self.upstream_not_started = upstream_not_started
        self.output_seen = output_seen
        self.tools_started = tools_started
        self.thread_bound = thread_bound


def _lane_classification(
    lane: str | None,
    model_id: str,
    row: dict[str, Any] | None = None,
) -> tuple[AccessClass, str, str] | None:
    if lane in _LANE_CLASSIFICATIONS:
        # An extended catalog can carry GLM rows while probing Codex's
        # built-in subscription provider. Never turn a GLM checkpoint into a
        # ChatGPT-plan candidate merely because the probe stamped the direct
        # lane onto it.
        if lane == "codex-direct" and (
            row is not None
            and (row.get("advisorProvider") == "glm" or model_id in GLM_DIRECT_MODELS)
        ):
            return None
        return _LANE_CLASSIFICATIONS[lane]
    if lane == "omniroute":
        # The host stamps this metadata after resolving the explicit Codex
        # OmniRoute connection.  A generic gateway label or a model spelling
        # alone is insufficient evidence for ChatGPT-plan access.
        provider = row.get("advisorProvider") if isinstance(row, dict) else None
        access_class = row.get("advisorAccessClass") if isinstance(row, dict) else None
        connection = row.get("advisorConnectionId") if isinstance(row, dict) else None
        if (
            provider == "openai"
            and access_class == "chatgpt_plan"
            and connection == "omniroute-codex-oauth"
            and model_id in _OPENAI_OMNIROUTE_TO_CANONICAL
        ):
            return "chatgpt_plan", "openai-codex-omniroute", connection
        # Retain the explicit GLM vocabulary for older authenticated hosts
        # that predate the advisor metadata fields.
        if model_id in GLM_OMNIROUTE_ROUTES:
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
class LogicalCatalogOption:
    """One canonical provider/checkpoint/effort choice for v2 clients."""

    choice: LogicalChoice
    display_name: str
    model_ids: tuple[str, ...]
    access_lanes: tuple[str, ...]
    available: bool = True
    unavailable_reason: str | None = None


@dataclass(frozen=True)
class HostCatalog:
    """The live qualified choice set for one host at one instant."""

    options: tuple[CatalogOption, ...]
    pool: PoolSnapshot
    candidates_by_id: dict[str, Candidate]
    logical_options: tuple[LogicalCatalogOption, ...] = ()
    logical_choices: tuple[LogicalChoice, ...] = ()
    routes_by_choice: dict[str, tuple[QualifiedRoute, ...]] = field(default_factory=dict)
    legacy_candidate_to_choice: dict[str, LogicalChoice] = field(default_factory=dict)

    @property
    def catalog_revision(self) -> str:
        return self.pool.catalog_revision

    @property
    def logical_catalog_revision(self) -> str:
        routes = [
            route.to_payload()
            for choice_id in sorted(self.routes_by_choice)
            for route in self.routes_by_choice[choice_id]
        ]
        return document_digest(routes)


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
    logical_by_id: dict[str, LogicalChoice] = {}
    logical_routes: dict[str, list[QualifiedRoute]] = {}
    logical_display_names: dict[str, str] = {}
    logical_aliases: dict[str, set[str]] = {}
    logical_lanes: dict[str, set[str]] = {}
    legacy_candidate_to_choice: dict[str, LogicalChoice] = {}
    for row in models:
        if not isinstance(row, dict):
            continue
        model_id = row.get("model") or row.get("id")
        lane = row.get("accessLane")
        if not isinstance(model_id, str) or not model_id.strip():
            continue
        classification = _lane_classification(
            lane if isinstance(lane, str) else None, model_id, row
        )
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
            try:
                choice_data = _logical_choice_for_row(
                    row, model_id=model_id.strip(), lane=candidate.lane_id, effort=effort
                )
                if choice_data is not None:
                    choice, route = choice_data
                    logical_by_id[choice.choice_id] = choice
                    current = logical_routes.setdefault(choice.choice_id, [])
                    if not any(
                        existing.transport == route.transport
                        and existing.route_id == route.route_id
                        and existing.wire_model == route.wire_model
                        and existing.wire_effort == route.wire_effort
                        and existing.connection_id == route.connection_id
                        and existing.entitlement_key == route.entitlement_key
                        and existing.equivalence_key == route.equivalence_key
                        for existing in current
                    ):
                        current.append(route)
                    logical_display_names.setdefault(choice.choice_id, display_name)
                    logical_aliases.setdefault(choice.choice_id, set()).add(model_id.strip())
                    logical_lanes.setdefault(choice.choice_id, set()).add(candidate.lane_id)
                    legacy_candidate_to_choice[candidate.candidate_id] = choice
            except ProviderPolicyError:
                # The v1 route can remain available to old rounds while an
                # unrecognized alias stays out of the v2 logical catalog.
                continue
    revision_source = sorted(candidate.route_identity for candidate in candidates)
    pool = PoolSnapshot(document_digest(revision_source), tuple(candidates))
    routes_by_choice = {
        choice_id: tuple(
            replace(route, catalog_revision=pool.catalog_revision) for route in routes
        )
        for choice_id, routes in logical_routes.items()
    }
    logical_choices = tuple(sorted(logical_by_id.values(), key=lambda item: item.choice_id))
    logical_options = tuple(
        LogicalCatalogOption(
            choice=choice,
            display_name=(
                glm_display_name(choice.model_id)
                if choice.provider == "glm"
                else logical_display_names[choice.choice_id]
            ),
            model_ids=tuple(sorted(logical_aliases[choice.choice_id])),
            access_lanes=tuple(sorted(logical_lanes[choice.choice_id])),
        )
        for choice in logical_choices
    )
    return HostCatalog(
        options=tuple(options),
        pool=pool,
        candidates_by_id={candidate.candidate_id: candidate for candidate in candidates},
        logical_options=logical_options,
        logical_choices=logical_choices,
        routes_by_choice={
            choice_id: tuple(sorted(routes, key=lambda route: (route.transport, route.wire_model)))
            for choice_id, routes in routes_by_choice.items()
        },
        legacy_candidate_to_choice=legacy_candidate_to_choice,
    )


def _logical_choice_for_row(
    row: dict[str, Any], *, model_id: str, lane: str, effort: str
) -> tuple[LogicalChoice, QualifiedRoute] | None:
    """Convert one authenticated physical row using explicit vocabularies."""
    classification = _lane_classification(lane, model_id, row)
    if classification is None:
        return None
    access_class, _provider_id, connection_id = classification
    provider: str
    canonical: str
    transport: str
    if access_class == "chatgpt_plan":
        provider = "openai"
        if lane == "omniroute":
            canonical = _OPENAI_OMNIROUTE_TO_CANONICAL[model_id]
            transport = "omniroute"
        else:
            canonical = _OPENAI_DIRECT_TO_CANONICAL.get(model_id, model_id)
            transport = "direct"
    else:
        provider = "glm"
        if lane == "omniroute":
            canonical = GLM_OMNIROUTE_ROUTES.get(model_id) or model_id.split("/", 1)[-1]
            transport = "omniroute"
        else:
            if model_id not in GLM_DIRECT_MODELS:
                return None
            canonical = model_id
            transport = "direct"
    if canonical.casefold() in {"auto", "default", "smart", "codex-auto-review"}:
        return None
    choice = LogicalChoice(provider, canonical, effort)
    entitlement_key = (
        row.get("advisorEntitlementKey")
        if isinstance(row.get("advisorEntitlementKey"), str)
        else (
            "chatgpt-plan:rtx-codex-owner" if provider == "openai" else "glm-plan:rtx-coding-plan"
        )
    )
    # Host metadata is trusted only because this row came from the
    # authenticated host catalog adapter. Keep the older lane-derived value
    # for pre-metadata hosts so v1 candidate identities remain unchanged.
    qualified_connection = row.get("advisorConnectionId")
    if not isinstance(qualified_connection, str) or not qualified_connection:
        qualified_connection = connection_id
    route = QualifiedRoute(
        choice=choice,
        transport=transport,
        route_id=lane,
        wire_model=model_id,
        wire_effort=effort,
        entitlement_kind="chatgpt_plan" if provider == "openai" else "glm_plan",
        entitlement_key=entitlement_key,
        equivalence_key=f"{provider}:codex-native:responses:{canonical}:{effort}",
        catalog_revision="host-catalog",
        ready=True,
        connection_id=qualified_connection,
    )
    return choice, route


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
        if record is None:
            return None
        logical_preferences: ProviderPreferences | None = None
        if record.payload.get("schema_version") == 1:
            # Migration is a read-time projection.  The v1 record and every
            # historical lane-pinned round remain untouched until the owner
            # explicitly saves the v2 settings after reviewing connections.
            try:
                catalog = await self.load_catalog(user_id, host_id)
                logical_preferences = migrate_v1(
                    record.payload,
                    catalog.legacy_candidate_to_choice,
                )
            except (HTTPException, AdvisorContractError, ProviderPolicyError):
                logical_preferences = None
        elif record.payload.get("schema_version") == 2:
            try:
                logical_preferences = ProviderPreferences.from_payload(record.payload)
            except ProviderPolicyError:
                logical_preferences = None
        return self._preferences_projection(record, logical_preferences=logical_preferences)

    async def save_preferences(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        preferences: AdvisorPreferences | ProviderPreferences,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        self._validated_profile(profile)
        self._authorized_host(user_id, host_id)
        if isinstance(preferences, ProviderPreferences):
            return await self._save_provider_preferences(
                user_id,
                host_id,
                profile,
                preferences,
                expected_version=expected_version,
            )
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

    async def _save_provider_preferences(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        preferences: ProviderPreferences,
        *,
        expected_version: int,
    ) -> dict[str, Any]:
        catalog = await self.load_catalog(user_id, host_id)
        by_id = {choice.choice_id: choice for choice in catalog.logical_choices}
        for provider in ("openai", "glm"):
            group = preferences.group(provider)
            for choice_id in group.selected_choice_ids:
                choice = by_id.get(choice_id)
                if choice is None or choice.provider != provider:
                    raise HTTPException(
                        status_code=422,
                        detail=f"Saved logical choice {choice_id!r} is not currently qualified",
                    )
        if (
            preferences.advisor_choice_id is not None
            and preferences.advisor_choice_id not in by_id
        ):
            raise HTTPException(
                status_code=422,
                detail="Advisor logical choice is not currently qualified",
            )
        try:
            record = await asyncio.to_thread(
                self.repository.save_provider_preferences,
                self.owner_id(user_id),
                host_id,
                preferences,
                expected_version=expected_version,
                profile=profile,
            )
        except AdvisorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return self._preferences_projection(record, logical_preferences=preferences)

    @staticmethod
    def _preferences_projection(
        record: Record,
        *,
        logical_preferences: ProviderPreferences | None = None,
    ) -> dict[str, Any]:
        payload = record.payload
        if payload.get("schema_version") == 2:
            stored_preferences: dict[str, Any] = dict(payload)
        else:
            stored_preferences = {
                "enabled": payload.get("enabled", False),
                "allowed_candidate_ids": list(payload.get("allowed_candidate_ids", ())),
                "advisor_candidate_id": payload.get("advisor_candidate_id"),
                "human_probability_percent": payload.get("human_probability_percent", 50),
            }
        return {
            "object": "model_advisor.preferences",
            "version": record.version,
            "etag": record.etag,
            "state": record.state,
            "preferences": stored_preferences,
            "logical_preferences": (
                logical_preferences.to_payload() if logical_preferences is not None else None
            ),
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

    async def _provider_preferences_for_round(
        self,
        owner: str,
        host_id: str,
        profile: str,
        explicit: ProviderPreferences | None = None,
    ) -> tuple[Record | None, ProviderPreferences]:
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
                detail=(
                    "No saved provider-grouped advisor settings for this host; save defaults first"
                ),
            )
        if record.payload.get("schema_version") != 2:
            raise HTTPException(
                status_code=422,
                detail=(
                    "Review the migrated provider settings and save them before "
                    "starting a v2 round"
                ),
            )
        try:
            preferences = ProviderPreferences.from_payload(record.payload)
        except ProviderPolicyError as exc:
            raise HTTPException(
                status_code=422, detail=f"Saved advisor settings are invalid: {exc}"
            ) from exc
        if not preferences.enabled:
            raise HTTPException(
                status_code=422, detail="The model advisor is disabled in saved settings"
            )
        return record, preferences

    async def _create_provider_round(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        *,
        task: str,
        human_choice_id: str,
        submission_key: str | None,
        preferences: ProviderPreferences | None,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        prefs_record, frozen_preferences = await self._provider_preferences_for_round(
            owner, host_id, profile, explicit=preferences
        )
        if not isinstance(task, str) or not task.strip() or len(task) > 200_000:
            raise HTTPException(status_code=422, detail="A nonempty initial task is required")
        catalog = await self.load_catalog(user_id, host_id)
        settings_revision = (
            f"prefs-v{prefs_record.version}-{prefs_record.etag}"
            if prefs_record is not None
            else "round-draft-" + document_digest(frozen_preferences.to_payload())
        )
        identity_key = submission_key or "implicit"
        submission_identity = document_digest(
            {
                "schema_version": 2,
                "owner": owner,
                "host": host_id,
                "profile": profile,
                "task": task,
                "human_choice_id": human_choice_id,
                "preferences": frozen_preferences.to_payload(),
                "submission_key": identity_key,
            }
        )
        round_id = "adviseround-" + submission_identity
        existing = await asyncio.to_thread(self.repository.load_round, owner, host_id, round_id)
        if existing is not None:
            return await self.load_round(user_id, host_id, round_id)
        try:
            frozen = freeze_logical_round(
                owner_id=owner,
                host_id=host_id,
                round_id=round_id,
                settings_revision=settings_revision,
                task=task,
                preferences=frozen_preferences,
                catalog=catalog.logical_choices,
                routes_by_choice=catalog.routes_by_choice,
                human_choice_id=human_choice_id,
            )
        except (LogicalAdvisorError, ProviderPolicyError) as exc:
            raise HTTPException(
                status_code=422,
                detail=f"Provider settings no longer match the live catalog: {exc}",
            ) from exc
        try:
            claim = await asyncio.to_thread(
                self.repository.reserve_provider_round, frozen, submission_key=submission_key
            )
        except AdvisorConflict as exc:
            existing = await asyncio.to_thread(
                self.repository.load_round, owner, host_id, round_id
            )
            if existing is not None:
                return await self.load_round(user_id, host_id, round_id)
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        if claim.acquired:
            self._schedule_provider_advisor(owner, host_id, round_id, frozen)
        return await self.load_round(user_id, host_id, round_id)

    def _schedule_provider_advisor(
        self, owner: str, host_id: str, round_id: str, frozen: LogicalFrozenRound
    ) -> None:
        """Run one v2 advisor call using the frozen physical transport plan."""

        async def _run() -> None:
            route = frozen.advisor_transport.primary

            async def record_attempt(status: str, *, reason: str | None = None) -> None:
                attempt = {
                    "phase": "advisor",
                    "transport": route.transport,
                    "route_id": route.route_id,
                    "connection_id": route.connection_id,
                    "model": route.wire_model,
                    "effort": route.wire_effort,
                    "status": status,
                }
                if reason:
                    attempt["reason"] = reason[:600]
                try:
                    await asyncio.to_thread(
                        self.repository.record_provider_transport_attempt,
                        owner,
                        host_id,
                        round_id,
                        attempt=attempt,
                    )
                except AdvisorConflict:
                    # A cancellation/confirmation race leaves the durable
                    # round authoritative; telemetry is best effort.
                    return

            await record_attempt("dispatch_started")
            try:
                from omnigent.server.routes._advisor_call import request_host_advisor_call

                conn = self._live_host_connection(self._host_store.get_host(host_id))
                result = await request_host_advisor_call(
                    host_registry=self._host_registry,
                    host_conn=conn,
                    request=frozen.advisor_input(),
                    model=route.wire_model,
                    access_lane=route.route_id,
                    reasoning_effort=(
                        None if route.wire_effort == "not_applicable" else route.wire_effort
                    ),
                    timeout_s=ADVISOR_FRAME_TIMEOUT_S,
                )
            except (HTTPException, ConnectionError, asyncio.TimeoutError) as exc:
                reason = f"advisor transport: {type(exc).__name__}"
                await record_attempt("failed", reason=reason)
                self._finish_advisor_failure(owner, host_id, round_id, reason)
                return
            if result.get("status") != "ok" or not isinstance(result.get("raw_output"), str):
                reason = str(result.get("error") or "advisor call failed on the host")
                await record_attempt("failed", reason=reason)
                self._finish_advisor_failure(owner, host_id, round_id, reason)
                return
            await record_attempt("completed")
            try:
                await asyncio.to_thread(
                    self.repository.finish_provider_advice,
                    owner,
                    host_id,
                    round_id,
                    str(result["raw_output"]),
                    randbelow=self._randbelow,
                    overhead=self._advisor_overhead(result),
                )
            except AdvisorConflict:
                return
            except (LogicalAdvisorError, ProviderPolicyError) as exc:
                self._finish_advisor_failure(
                    owner, host_id, round_id, f"advisor output rejected: {exc}"
                )

        def _log_crash(task: asyncio.Task[None]) -> None:
            if not task.cancelled() and task.exception() is not None:
                _logger.exception(
                    "provider advisor background call crashed", exc_info=task.exception()
                )
            self._background_tasks.discard(task)

        task = asyncio.create_task(_run())
        self._background_tasks.add(task)
        task.add_done_callback(_log_crash)

    async def create_round(
        self,
        user_id: str | None,
        host_id: str,
        profile: str,
        *,
        task: str,
        human_candidate_id: str | None = None,
        human_choice_id: str | None = None,
        submission_key: str | None = None,
        preferences: AdvisorPreferences | ProviderPreferences | None = None,
    ) -> dict[str, Any]:
        if isinstance(preferences, ProviderPreferences) or human_choice_id is not None:
            if not isinstance(human_choice_id, str) or not human_choice_id:
                raise HTTPException(status_code=422, detail="A logical human choice is required")
            return await self._create_provider_round(
                user_id,
                host_id,
                profile,
                task=task,
                human_choice_id=human_choice_id,
                submission_key=submission_key,
                preferences=(
                    preferences if isinstance(preferences, ProviderPreferences) else None
                ),
            )
        owner = self.owner_id(user_id)
        self._validated_profile(profile)
        self._authorized_host(user_id, host_id)
        prefs_record, frozen_preferences = await self._preferences_for_round(
            owner, host_id, profile, explicit=preferences
        )
        if not isinstance(task, str) or not task.strip() or len(task) > 200_000:
            raise HTTPException(status_code=422, detail="A nonempty initial task is required")
        if (
            not isinstance(human_candidate_id, str)
            or human_candidate_id not in frozen_preferences.allowed_candidate_ids
        ):
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
            if payload.get("schema_version") == 2:
                decision = LogicalReviewDecision.from_payload(review)
                projection["review"] = {
                    "schema_version": 2,
                    "round_fingerprint": decision.round_fingerprint,
                    "human_choice_id": decision.human_choice_id,
                    "advisor_choice_id": decision.advisor_choice_id,
                    "human_candidate_id": decision.human_choice_id,
                    "advisor_candidate_id": decision.advisor_choice_id,
                    "rationale": decision.rationale,
                    "assigned_choice_id": decision.original_assignment.selected_choice_id,
                    "assigned_candidate_id": decision.original_assignment.selected_choice_id,
                    "assigned_arm": decision.original_assignment.arm,
                    "human_probability_percent": decision.human_probability_percent,
                    "overridden": decision.overridden,
                    "override_reason": decision.override_reason,
                    "comparison_group": decision.comparison_group,
                }
            else:
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
        if payload.get("schema_version") == 2:
            if isinstance(payload.get("execution_plan"), dict):
                projection["execution_plan"] = payload["execution_plan"]
            if isinstance(payload.get("transport_attempts"), list):
                projection["transport_attempts"] = payload["transport_attempts"]
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
        existing = await asyncio.to_thread(self.repository.load_round, owner, host_id, round_id)
        if existing is not None and existing.payload.get("schema_version") == 2:
            return await self._confirm_provider_round(
                user_id,
                host_id,
                round_id,
                expected_version=expected_version,
                override_choice_id=override_candidate_id,
                reason=reason,
                launch=launch,
                request=request,
            )
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

    async def _confirm_provider_round(
        self,
        user_id: str | None,
        host_id: str,
        round_id: str,
        *,
        expected_version: int,
        override_choice_id: str | None,
        reason: str | None,
        launch: dict[str, Any],
        request: Any,
    ) -> dict[str, Any]:
        owner = self.owner_id(user_id)
        record = await asyncio.to_thread(self.repository.load_round, owner, host_id, round_id)
        if record is None:
            raise HTTPException(status_code=404, detail="advisor round not found")
        try:
            frozen = LogicalFrozenRound.from_payload(record.payload["frozen"])
            review = LogicalReviewDecision.from_payload(record.payload["review"])
        except (KeyError, LogicalAdvisorError, ProviderPolicyError) as exc:
            raise HTTPException(
                status_code=422, detail=f"Logical round is invalid: {exc}"
            ) from exc
        catalog = await self.load_catalog(user_id, host_id)
        execution_choice_id = override_choice_id or review.execution_choice_id
        choice_by_id = {choice.choice_id: choice for choice in frozen.pool}
        choice = choice_by_id.get(execution_choice_id)
        if choice is None:
            raise HTTPException(
                status_code=422, detail="Execution choice is outside the frozen pool"
            )
        try:
            plan = plan_transport(
                choice,
                frozen.preference_for(choice.provider),
                catalog.routes_by_choice.get(choice.choice_id, ()),
            )
            execution_plan = plan.to_payload()
            claim = await asyncio.to_thread(
                self.repository.confirm_provider,
                owner,
                host_id,
                round_id,
                expected_version=expected_version,
                execution_plan=execution_plan,
                override_choice_id=override_choice_id,
                reason=reason,
            )
        except AdvisorConflict as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except (LogicalAdvisorError, ProviderPolicyError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not claim.acquired:
            return await self.load_round(user_id, host_id, round_id)
        try:
            session_id, bound_route = await self._launch_provider_executor(
                user_id,
                host_id,
                plan,
                frozen,
                review,
                launch,
                request,
            )
        except Exception as exc:
            _logger.exception("Provider-grouped advisor launch failed for round %s", round_id)
            # The launch path may have appended an uncertain transport
            # attempt after the CAS claim. Reload so the response does not
            # hide that provenance behind the pre-launch claim snapshot.
            projection = await self.load_round(user_id, host_id, round_id)
            projection["launch_error"] = str(exc)
            return projection
        try:
            await asyncio.to_thread(
                self.repository.bind_dispatch, owner, host_id, round_id, session_id=session_id
            )
            await asyncio.to_thread(
                self.repository.record_provider_transport_attempt,
                owner,
                host_id,
                round_id,
                attempt={
                    "phase": "answer",
                    "transport": bound_route.transport,
                    "route_id": bound_route.route_id,
                    "connection_id": bound_route.connection_id,
                    "model": bound_route.wire_model,
                    "effort": bound_route.wire_effort,
                    "status": "session_bound",
                },
            )
        except AdvisorConflict as exc:
            _logger.error(
                "Provider advisor session binding failed for round %s: %s", round_id, exc
            )
        return await self.load_round(user_id, host_id, round_id)

    async def _launch_provider_executor(
        self,
        user_id: str | None,
        host_id: str,
        plan: TransportPlan,
        frozen: LogicalFrozenRound,
        review: LogicalReviewDecision,
        launch: dict[str, Any],
        request: Any,
    ) -> tuple[str, QualifiedRoute]:
        """Create one logical round session and carry its route contract."""
        from omnigent.server.schemas import SessionCreateRequest, SessionEventInput

        if not isinstance(launch.get("agent_id"), str) or not launch.get("agent_id"):
            raise RuntimeError("An agent_id is required to launch the round")
        workspace = launch.get("workspace")
        if not isinstance(workspace, str) or not workspace:
            raise RuntimeError("A workspace is required to launch the round")

        def body_for(route: QualifiedRoute) -> SessionCreateRequest:
            labels = {
                ADVISOR_ROUND_LABEL_KEY: frozen.round_id,
                ADVISOR_ROUND_FINGERPRINT_LABEL_KEY: frozen.fingerprint,
                ADVISOR_ROUND_GROUP_LABEL_KEY: review.comparison_group,
                ADVISOR_LOGICAL_CHOICE_LABEL_KEY: plan.choice.choice_id,
                ADVISOR_TRANSPORT_PLAN_LABEL_KEY: json.dumps(
                    plan.to_payload(), sort_keys=True, separators=(",", ":")
                ),
                # The full plan remains the immutable audit contract; this
                # per-attempt route identifies which qualified leg the runner
                # must bind for this concrete session.
                ADVISOR_DISPATCH_ROUTE_LABEL_KEY: json.dumps(
                    route.to_payload(), sort_keys=True, separators=(",", ":")
                ),
                # The lane is the transport family; the attested connection
                # binds the selected account/plan within that family.
                ADVISOR_CONNECTION_LABEL_KEY: route.connection_id or "",
            }
            if route.route_id:
                labels["omnigent.access_lane"] = route.route_id
            return SessionCreateRequest(
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
                model_override=route.wire_model,
                reasoning_effort=(
                    None if route.wire_effort == "not_applicable" else route.wire_effort
                ),
                terminal_launch_args=launch.get("terminal_launch_args"),
            )

        await asyncio.to_thread(
            self.repository.record_provider_transport_attempt,
            frozen.owner_id,
            frozen.host_id,
            frozen.round_id,
            attempt={
                "phase": "answer",
                "transport": plan.primary.transport,
                "route_id": plan.primary.route_id,
                "connection_id": plan.primary.connection_id,
                "model": plan.primary.wire_model,
                "effort": plan.primary.wire_effort,
                "status": "dispatch_started",
            },
        )
        try:
            session = await self._session_launcher(
                body_for(plan.primary),
                user_id=user_id,
                request=request,
            )
            return session.id, plan.primary
        except PreDispatchRouteFailure as exc:
            fallback = permitted_fallback(
                plan,
                cause=exc.cause,
                upstream_not_started=exc.upstream_not_started,
                output_seen=exc.output_seen,
                tools_started=exc.tools_started,
                thread_bound=exc.thread_bound,
            )
            await asyncio.to_thread(
                self.repository.record_provider_transport_attempt,
                frozen.owner_id,
                frozen.host_id,
                frozen.round_id,
                attempt={
                    "phase": "answer",
                    "transport": plan.primary.transport,
                    "route_id": plan.primary.route_id,
                    "connection_id": plan.primary.connection_id,
                    "model": plan.primary.wire_model,
                    "effort": plan.primary.wire_effort,
                    "status": "failed_before_upstream",
                    "reason": exc.cause,
                },
            )
            if fallback is None:
                raise
            # Re-read the host catalog before crossing the route boundary.
            # A stale snapshot cannot authorize a different account or wire
            # model merely because the first gateway leg failed.
            current_catalog = await self.load_catalog(user_id, host_id)
            current_fallback = next(
                (
                    route
                    for route in current_catalog.routes_by_choice.get(plan.choice.choice_id, ())
                    if route.transport == fallback.transport
                    and route.wire_model == fallback.wire_model
                    and route.wire_effort == fallback.wire_effort
                    and route.connection_id == fallback.connection_id
                    and route.entitlement_key == fallback.entitlement_key
                    and route.equivalence_key == fallback.equivalence_key
                    and route.ready
                ),
                None,
            )
            if current_fallback is None:
                await asyncio.to_thread(
                    self.repository.record_provider_transport_attempt,
                    frozen.owner_id,
                    frozen.host_id,
                    frozen.round_id,
                    attempt={
                        "phase": "answer",
                        "transport": fallback.transport,
                        "route_id": fallback.route_id,
                        "connection_id": fallback.connection_id,
                        "model": fallback.wire_model,
                        "effort": fallback.wire_effort,
                        "status": "fallback_blocked_binding_changed",
                        "reason": "qualified_direct_binding_changed",
                    },
                )
                raise RuntimeError(
                    "Qualified Direct fallback changed before dispatch; refusing replay"
                ) from exc
            try:
                fallback_session = await self._session_launcher(
                    body_for(current_fallback),
                    user_id=user_id,
                    request=request,
                )
            except Exception as exc:
                await asyncio.to_thread(
                    self.repository.record_provider_transport_attempt,
                    frozen.owner_id,
                    frozen.host_id,
                    frozen.round_id,
                    attempt={
                        "phase": "answer",
                        "transport": current_fallback.transport,
                        "route_id": current_fallback.route_id,
                        "connection_id": current_fallback.connection_id,
                        "model": current_fallback.wire_model,
                        "effort": current_fallback.wire_effort,
                        "status": "fallback_dispatch_uncertain",
                        "reason": type(exc).__name__,
                    },
                )
                raise
            await asyncio.to_thread(
                self.repository.record_provider_transport_attempt,
                frozen.owner_id,
                frozen.host_id,
                frozen.round_id,
                attempt={
                    "phase": "answer",
                    "transport": current_fallback.transport,
                    "route_id": current_fallback.route_id,
                    "connection_id": current_fallback.connection_id,
                    "model": current_fallback.wire_model,
                    "effort": current_fallback.wire_effort,
                    "status": "fallback_session_created",
                    "reason": "safe_pre_dispatch_fallback",
                },
            )
            return fallback_session.id, current_fallback
        except Exception as exc:
            # A non-typed failure may have crossed the provider boundary. Keep
            # the dispatch claim uncertain and never replay it automatically.
            await asyncio.to_thread(
                self.repository.record_provider_transport_attempt,
                frozen.owner_id,
                frozen.host_id,
                frozen.round_id,
                attempt={
                    "phase": "answer",
                    "transport": plan.primary.transport,
                    "route_id": plan.primary.route_id,
                    "connection_id": plan.primary.connection_id,
                    "model": plan.primary.wire_model,
                    "effort": plan.primary.wire_effort,
                    "status": "dispatch_uncertain",
                    "reason": type(exc).__name__,
                },
            )
            raise

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
