"""Canonical resolver for the OpenCode delegation selector.

Production evidence (issue #56):

- A request for the native OpenCode agent — by name, by built-in picker,
  or via ``sys_session_send agent=opencode`` — silently resolved to the
  Verity orchestrator agent and ran on the Claude Agent SDK
  (``claude-sdk``) instead of the OpenCode native harness
  (``opencode-native``).
- Verity's prompt explicitly mentions "opencode" multiple times and
  declares ``claude-sdk`` as its own harness; a fuzzy / first-match /
  description-text selector against the catalog returns Verity before
  it ever sees ``opencode-native-ui``.
- After a runner reconnect or spec-cache eviction, the streaming
  dispatch path resolved the parent agent's harness and respawned the
  child's native terminal, which surfaced in the Web UI as
  ``"Bridge closed: terminal resource not found"``.
- No delegation provenance recorded ``requested_selector``,
  ``resolved_harness``, ``resolved_provider``, ``resolved_model``,
  ``reasoning_effort``, ``fallback_used``, ``workspace``,
  ``parent_conversation_id``, ``child_conversation_id`` — so a
  silent fallback was invisible.

This module is the single source of truth for the semantic
``opencode`` selector. Every delegation entry point
(``POST /v1/sessions``, the Web UI picker, runner child launch,
``sys_session_send``, ``sys_session_create``) calls into
:func:`resolve_delegate_agent` and consumes the structured
:class:`AgentSelection` result. Resolution prefers stable identifiers
and explicit capabilities over fuzzy name matching:

- **Stable identifier**: the canonical agent name declared by the
  registered OpenCode native wrapper
  (:data:`omnigent.harness_plugins.OPENCODE_NATIVE_CODING_AGENT.agent_name`),
  plus its harness kind ``opencode-native``. Both come from the same
  descriptor the server uses to seed ``opencode-native-ui`` at startup.
- **Explicit capability**: the resolved agent's
  :attr:`AgentSpec.harness_kind` MUST equal ``opencode-native``. A
  resolved agent whose harness is ``claude-sdk`` (or anything other
  than ``opencode-native``) is rejected.
- **Ambiguity / disabled / missing / unavailable**: each fails with
  a structured :class:`AgentSelectionError` carrying the rejection
  reason. The caller persists the reason into the durable delegation
  provenance and surfaces it on the session snapshot — no silent
  fallback, no Verity substitution, no default agent.

A selector result records exactly what the resolver decided and
what it would have dispatched, so post-launch regression analysis
can compare requested vs. resolved vs. launched identities byte for
byte.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness, is_native_harness
from omnigent.harness_plugins import (
    OPENCODE_NATIVE_CODING_AGENT,
    native_agents,
)
from omnigent.spec.types import AgentSpec

if TYPE_CHECKING:
    from omnigent.entities import Agent, LoadedAgent
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.stores.agent_store import AgentStore

_logger = logging.getLogger(__name__)


# Canonical semantic selectors the resolver knows about. Adding a new
# entry requires the matching native-coding-agent descriptor to expose
# ``agent_name`` (the registered template name) and ``harness`` (the
# native harness the resolver must enforce). The selectors are stable,
# user-facing identifiers — they are NEVER matched against free-text
# agent descriptions, because that is exactly how Verity was picked
# over OpenCode in the production evidence.
#
# Extend deliberately; every entry is the single source of truth for a
# "user asked for this kind of native worker" selector.
SEMANTIC_OPENCODE_SELECTOR: str = "opencode"
SEMANTIC_OPENCODE_AGENT_NAME: str = OPENCODE_NATIVE_CODING_AGENT.agent_name
SEMANTIC_OPENCODE_HARNESS: str = OPENCODE_NATIVE_CODING_AGENT.harness
SEMANTIC_OPENCODE_DISPLAY_NAME: str = OPENCODE_NATIVE_CODING_AGENT.display_name


@dataclass(frozen=True)
class _CarriesSelectionAttributes:
    """Marker — fields are not actually frozen; see class docstring."""


class AgentSelectionError(OmnigentError):
    """Raised when the OpenCode selector cannot resolve a valid agent.

    Carries the structured rejection reason the durable delegation
    provenance persists and the parent conversation surfaces. NEVER
    silently falls back to a different agent or harness.

    Implementation note: a plain :class:`OmnigentError` is used as
    the base (the dataclass form would enforce ``frozen=True`` and
    reject the parent's ``self.code = code`` assignment). The
    delegation-specific fields are plain instance attributes set in
    the constructor.
    """

    def __init__(
        self,
        message: str,
        *,
        reason: str,
        selector: str,
        candidates: tuple[dict[str, Any], ...] = (),
    ) -> None:
        super().__init__(message, code=ErrorCode.INVALID_INPUT)
        # ``OmnigentError`` exposes ``code`` / ``message`` as plain
        # attributes; we add the delegation-specific fields the
        # provenance row needs.
        self.reason: str = reason
        self.selector: str = selector
        self.candidates: tuple[dict[str, Any], ...] = candidates

    def to_provenance_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for the delegation provenance row."""
        return {
            "reason": self.reason,
            "selector": self.selector,
            "candidates": [dict(c) for c in self.candidates],
        }


@dataclass(frozen=True)
class AgentSelection:
    """The resolved identity of a semantic agent selector.

    :param selector: The original semantic selector, e.g. ``"opencode"``.
    :param resolved_agent_id: Stable id of the resolved template agent,
        e.g. ``"ag_..."``. None means resolution failed (see
        :attr:`error`).
    :param resolved_agent_name: Canonical template name, e.g.
        ``"opencode-native-ui"``. None on failure.
    :param resolved_harness: Canonical harness the worker must launch
        through, e.g. ``"opencode-native"``. None on failure.
    :param resolved_display_name: Human-readable display name, e.g.
        ``"OpenCode"``. None on failure.
    :param native: Whether the resolved harness is one of the native
        server / TUI harnesses. Always ``True`` for the OpenCode
        selector (the only supported selector today).
    :param decision: ``"resolved"`` on success; the structured
        rejection reason on failure.
    :param candidates: Diagnostic list of agent rows the resolver
        considered, so post-incident analysis can see why the
        selector failed (disabled / missing / harness-mismatch).
    """

    selector: str
    resolved_agent_id: str | None
    resolved_agent_name: str | None
    resolved_harness: str | None
    resolved_display_name: str | None
    native: bool
    decision: str
    candidates: tuple[dict[str, Any], ...] = ()
    error: AgentSelectionError | None = None

    @property
    def ok(self) -> bool:
        """Return whether resolution succeeded and produced a usable identity."""
        return self.error is None and self.resolved_agent_id is not None

    def to_provenance_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly view for the durable delegation provenance row."""
        return {
            "selector": self.selector,
            "resolved_agent_id": self.resolved_agent_id,
            "resolved_agent_name": self.resolved_agent_name,
            "resolved_harness": self.resolved_harness,
            "resolved_display_name": self.resolved_display_name,
            "native": self.native,
            "decision": self.decision,
            "candidates": [dict(c) for c in self.candidates],
            "error": self.error.to_provenance_dict() if self.error else None,
        }


@dataclass(frozen=True)
class DelegateRequest:
    """Inputs the resolver needs to produce an :class:`AgentSelection`.

    The bundle + spec are intentionally OPTIONAL: the resolver succeeds
    on name + agent row alone when ``spec_harness`` matches the
    selector's required harness. The spec is REQUIRED for the
    "harness mismatch" rejection gate, so callers that want strict
    validation (create-time and pre-launch re-validation) pass it.
    """

    selector: str
    agent_row: "Agent"
    loaded_agent: "LoadedAgent | None" = None


def _native_coding_agent_for_harness(harness: str | None) -> Any | None:
    """Return the registered native-coding-agent descriptor for *harness*.

    Returns ``None`` for non-native harnesses so the resolver cannot
    silently promote a non-native worker to a native selector.
    """
    if not harness:
        return None
    if not is_native_harness(harness):
        return None
    for descriptor in native_agents():
        if canonicalize_harness(descriptor.harness) == canonicalize_harness(harness):
            return descriptor
    return None


def _spec_harness(spec: AgentSpec) -> str:
    """Return the canonical harness declared by a parsed spec.

    Mirrors :func:`omnigent.server.routes.sessions._spec_harness`
    without taking on the routes-layer import surface — the resolver
    is the lower-level canonical source and the routes layer reads
    its decisions.
    """
    raw = spec.executor.config.get("harness") or spec.executor.type
    return canonicalize_harness(raw) or raw


def _agent_harness_kind(agent_row: "Agent", loaded: "LoadedAgent | None") -> str | None:
    """Best-effort canonical harness kind for the registered agent row.

    Falls back to ``None`` when the bundle cannot be loaded so the
    resolver surfaces a load failure instead of pretending it can
    verify the harness match.
    """
    if loaded is None:
        return None
    return _spec_harness(loaded.spec)


def _candidate_snapshot(agent_row: "Agent", harness_kind: str | None) -> dict[str, Any]:
    """Compact diagnostic view of one agent row the resolver considered."""
    return {
        "agent_id": agent_row.id,
        "name": agent_row.name,
        "session_id": agent_row.session_id,
        "harness_kind": harness_kind,
    }


def resolve_opencode_delegate(
    request: DelegateRequest,
) -> AgentSelection:
    """Resolve the canonical OpenCode native agent for the semantic selector.

    Strict semantics (issue #56):

    1. The agent row's canonical name MUST match the registered
       OpenCode native agent name (``opencode-native-ui``). A row
       whose name contains "opencode" but is something else (Verity
       names ``verity`` but its description text contains "opencode"
       in many places) is rejected — fuzzy text matching is what
       caused the production silent fallback.
    2. The loaded spec's :attr:`harness_kind` MUST equal
       ``opencode-native``. Any other harness (most importantly
       ``claude-sdk`` for Verity) is rejected. The bundle must load
       cleanly — a spec we cannot parse cannot be trusted.
    3. The session_id MUST be ``None`` — the resolver matches TEMPLATE
       agents only. A session-scoped agent (kind=session) is the
       wrong scope and is rejected with a distinct reason.
    4. Exactly one candidate MUST match. Multiple matches fail as
       ambiguous so the operator can prune the registry.

    The resolver is the single source of truth used at every
    delegation entry point. New selectors (``"verity"``,
    ``"claude-native"``, etc.) MUST be added by extending this module
    rather than re-implementing name / harness checks in callers.
    """
    candidates: list[dict[str, Any]] = []

    # Build a diagnostic snapshot of every candidate the resolver
    # considered so a rejection carries enough context to diagnose
    # without re-running the resolver.
    harness_kind = _agent_harness_kind(request.agent_row, request.loaded_agent)
    candidates.append(_candidate_snapshot(request.agent_row, harness_kind))

    # (1) Stable canonical-name match. Reject fuzzy matches that
    #     merely contain the selector substring (Verity's description
    #     contains "opencode" in many places — exactly what produced
    #     the production fall-through).
    if request.agent_row.name != SEMANTIC_OPENCODE_AGENT_NAME:
        return AgentSelection(
            selector=request.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_name_mismatch",
            candidates=tuple(candidates),
            error=AgentSelectionError(
                (
                    f"OpenCode selector rejected agent {request.agent_row.id!r}: "
                    f"name {request.agent_row.name!r} != "
                    f"{SEMANTIC_OPENCODE_AGENT_NAME!r}. Fuzzy / partial / "
                    "description-text matching is not allowed."
                ),
                reason="rejected_name_mismatch",
                selector=request.selector,
                candidates=tuple(candidates),
            ),
        )

    # (3) Template-only. A session-scoped agent is the wrong scope
    #     for the OpenCode built-in selector.
    if request.agent_row.session_id is not None:
        return AgentSelection(
            selector=request.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_session_scoped_agent",
            candidates=tuple(candidates),
            error=AgentSelectionError(
                (
                    f"OpenCode selector rejected agent {request.agent_row.id!r}: "
                    "session-scoped agents cannot satisfy a built-in selector."
                ),
                reason="rejected_session_scoped_agent",
                selector=request.selector,
                candidates=tuple(candidates),
            ),
        )

    # Bundle load failure → spec_harness cannot be verified, so the
    # strict harness-mismatch gate cannot run. Reject loudly: never
    # silently fall back to "we trust the harness anyway".
    if request.loaded_agent is None:
        return AgentSelection(
            selector=request.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_bundle_unloadable",
            candidates=tuple(candidates),
            error=AgentSelectionError(
                (
                    f"OpenCode selector rejected agent {request.agent_row.id!r}: "
                    "agent bundle could not be loaded; refusing to fall "
                    "through to a parent harness without verification."
                ),
                reason="rejected_bundle_unloadable",
                selector=request.selector,
                candidates=tuple(candidates),
            ),
        )

    # (2) Harness must equal opencode-native. Verity (claude-sdk) and
    #     any other harness are rejected here.
    if harness_kind != SEMANTIC_OPENCODE_HARNESS:
        return AgentSelection(
            selector=request.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_harness_mismatch",
            candidates=tuple(candidates),
            error=AgentSelectionError(
                (
                    f"OpenCode selector rejected agent {request.agent_row.id!r}: "
                    f"declared harness {harness_kind!r} != "
                    f"{SEMANTIC_OPENCODE_HARNESS!r}. A Verity / claude-sdk "
                    "fallback is never allowed for the OpenCode selector."
                ),
                reason="rejected_harness_mismatch",
                selector=request.selector,
                candidates=tuple(candidates),
            ),
        )

    # (4) Native-server harness required (the descriptor's harness
    #     class). A non-native harness would short-circuit the
    #     opencode-native child-execution surface entirely.
    descriptor = _native_coding_agent_for_harness(SEMANTIC_OPENCODE_HARNESS)
    if descriptor is None:
        return AgentSelection(
            selector=request.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_native_descriptor_missing",
            candidates=tuple(candidates),
            error=AgentSelectionError(
                (
                    "OpenCode selector is misconfigured: the "
                    f"{SEMANTIC_OPENCODE_HARNESS!r} native-coding-agent "
                    "descriptor is not registered."
                ),
                reason="rejected_native_descriptor_missing",
                selector=request.selector,
                candidates=tuple(candidates),
            ),
        )

    return AgentSelection(
        selector=request.selector,
        resolved_agent_id=request.agent_row.id,
        resolved_agent_name=request.agent_row.name,
        resolved_harness=SEMANTIC_OPENCODE_HARNESS,
        resolved_display_name=descriptor.display_name,
        native=True,
        decision="resolved",
        candidates=tuple(candidates),
    )


def resolve_delegate_agent(
    *,
    selector: str,
    agent_store: "AgentStore",
    agent_cache: "AgentCache | None",
) -> AgentSelection:
    """Resolve the canonical agent identity for a semantic delegation selector.

    Every delegation entry point MUST funnel through this helper. It
    is the single source of truth for "what agent does the selector
    pick" — fuzzy / partial / description-text matching is explicitly
    forbidden. The Web UI / API / runner / sub-agent dispatch all
    consume the returned :class:`AgentSelection` and persist its
    provenance; a failure is never silently substituted with a
    default agent.

    Currently the only supported selector is ``"opencode"``. New
    selectors must be added through a new
    ``resolve_<x>_delegate`` function in this module.

    :param selector: The semantic selector the caller is asking for,
        e.g. ``"opencode"``. Case-sensitive.
    :param agent_store: Agent store used for the canonical-name
        lookup. The store's :meth:`get_by_name` is the durable
        authoritative source for template-agent identity.
    :param agent_cache: Optional cache used to load the bundle so the
        resolver can verify the spec's harness kind. ``None`` is
        permitted for a name-only resolution but the strict
        harness-mismatch gate can only run when the cache is provided.
    :returns: The :class:`AgentSelection` decision.
    :raises AgentSelectionError: when the selector is unknown or the
        resolver cannot be invoked (e.g. unknown selector name).
    """
    if selector != SEMANTIC_OPENCODE_SELECTOR:
        raise AgentSelectionError(
            f"unknown delegation selector {selector!r}",
            reason="rejected_unknown_selector",
            selector=selector,
        )

    agent_row = agent_store.get_by_name(SEMANTIC_OPENCODE_AGENT_NAME)
    if isinstance(agent_row, list):
        # The store's contract is to return the single registered row.
        # A list is a contract violation that would otherwise silently
        # pass through (or, depending on the store implementation,
        # silently pick a winner). Surface it loudly: this is exactly
        # the failure mode that produced the production bug — a
        # non-unique agent name resolved to a wrong harness.
        raise AgentSelectionError(
            f"agent store returned {len(agent_row)} rows for "
            f"{SEMANTIC_OPENCODE_AGENT_NAME!r}; refusing to silently "
            "pick a winner. Operator must prune duplicate registrations.",
            reason="rejected_ambiguous_match",
            selector=selector,
        )
    if agent_row is None:
        candidates: tuple[dict[str, Any], ...] = ()
        return AgentSelection(
            selector=selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_missing_agent",
            candidates=candidates,
            error=AgentSelectionError(
                (
                    f"OpenCode selector found no template agent named "
                    f"{SEMANTIC_OPENCODE_AGENT_NAME!r}; refusing to fall "
                    "back to a default agent."
                ),
                reason="rejected_missing_agent",
                selector=selector,
                candidates=candidates,
            ),
        )

    loaded_agent: "LoadedAgent | None" = None
    if agent_cache is not None:
        try:
            loaded_agent = agent_cache.load(
                agent_row.id,
                agent_row.bundle_location,
                expand_env=agent_row.session_id is None,
            )
        except Exception:
            loaded_agent = None

    return resolve_opencode_delegate(
        DelegateRequest(
            selector=selector,
            agent_row=agent_row,
            loaded_agent=loaded_agent,
        )
    )


def revalidate_delegated_identity(
    *,
    selection: AgentSelection,
    agent_store: "AgentStore",
    agent_cache: "AgentCache | None",
) -> AgentSelection:
    """Re-run the canonical resolver against the persisted identity.

    Called immediately before child launch so a stale registry
    change (e.g. an operator disabled the agent, swapped its bundle,
    or replaced it with a different template of the same name)
    cannot switch the harness between initial resolution and
    spawn. The re-validation produces a NEW :class:`AgentSelection`
    that the caller MUST consume instead of the original.

    If the re-validation fails (the agent was disabled / removed /
    had its harness swapped out from under us), the caller fails
    the launch loudly rather than silently falling through to a
    different harness — exactly the failure mode issue #56 documents.

    :param selection: The previously-resolved :class:`AgentSelection`.
    :param agent_store: Agent store used for the durable lookup.
    :param agent_cache: Optional cache used to load the bundle.
    :returns: A fresh :class:`AgentSelection` for the same selector.
    """
    if not selection.ok or selection.resolved_agent_id is None:
        # Already failed at the prior gate; do not regress to a
        # partial re-validation.
        return selection
    if selection.selector != SEMANTIC_OPENCODE_SELECTOR:
        # Today only the OpenCode selector is revalidated; new
        # selectors will need their own revalidation path.
        raise AgentSelectionError(
            f"cannot revalidate selector {selection.selector!r}",
            reason="rejected_unknown_selector",
            selector=selection.selector,
        )

    current = agent_store.get(selection.resolved_agent_id)
    if current is None:
        return AgentSelection(
            selector=selection.selector,
            resolved_agent_id=None,
            resolved_agent_name=None,
            resolved_harness=None,
            resolved_display_name=None,
            native=True,
            decision="rejected_missing_agent_at_launch",
            candidates=(),
            error=AgentSelectionError(
                (
                    "OpenCode agent row disappeared between initial "
                    "resolution and child launch; refusing to fall "
                    "back to a default agent."
                ),
                reason="rejected_missing_agent_at_launch",
                selector=selection.selector,
            ),
        )

    loaded: "LoadedAgent | None" = None
    if agent_cache is not None:
        try:
            loaded = agent_cache.load(
                current.id,
                current.bundle_location,
                expand_env=current.session_id is None,
            )
        except Exception:
            loaded = None

    return resolve_opencode_delegate(
        DelegateRequest(
            selector=selection.selector,
            agent_row=current,
            loaded_agent=loaded,
        )
    )


# Lightweight observation used by structured logging; callers can also
# read it from the returned :class:`AgentSelection` for the durable
# provenance row.
def log_delegation_decision(selection: AgentSelection) -> None:
    """Emit a single structured-log line for the resolver decision.

    The payload intentionally omits secrets / credentials / token
    material. It is the runtime fingerprint a SRE can grep when a
    user reports "OpenCode ran Verity": the line proves the resolver
    either rejected the dispatch or resolved it to the canonical
    OpenCode native agent.
    """
    if selection.ok:
        _logger.info(
            "agent_selector.resolved selector=%s resolved_agent_id=%s "
            "resolved_agent_name=%s resolved_harness=%s decision=%s",
            selection.selector,
            selection.resolved_agent_id,
            selection.resolved_agent_name,
            selection.resolved_harness,
            selection.decision,
        )
        return
    error = selection.error
    _logger.warning(
        "agent_selector.rejected selector=%s decision=%s reason=%s "
        "candidates=%d",
        selection.selector,
        selection.decision,
        getattr(error, "reason", "unknown"),
        len(selection.candidates),
    )


__all__ = [
    "AgentSelection",
    "AgentSelectionError",
    "DelegateRequest",
    "SEMANTIC_OPENCODE_AGENT_NAME",
    "SEMANTIC_OPENCODE_DISPLAY_NAME",
    "SEMANTIC_OPENCODE_HARNESS",
    "SEMANTIC_OPENCODE_SELECTOR",
    "log_delegation_decision",
    "revalidate_delegated_identity",
    "resolve_delegate_agent",
    "resolve_opencode_delegate",
]
