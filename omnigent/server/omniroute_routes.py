"""Native OmniRoute route catalog used by Omnigent routing approval.

This module is the single source of truth for which OmniRoute route ids
Omnigent accepts as **interactive execution** routes. The contract:

* ``auto/*`` ids (built-in virtual Auto Combos).
* ``custom/best-coding`` — the canonical interactive execution combo
  persisted in ``omniroute-customizations/combos/custom-best-coding.json``.
  It exists outside the static catalog because it's a custom persisted
  Combo, not a virtual Auto Combo, but it is what the web UI's model
  picker calls "OmniRoute Coding Best" for interactive execution.
* ``custom/outcome-scoring`` is **deliberately** NOT in this catalog:
  it is the M3-only background Task Outcome evaluator route and must
  not be user-selectable for interactive execution.

The display label (``OmniRoute Coding Best``) is never accepted as a
route id; only the canonical wire id (``custom/best-coding``) is.
"""

from __future__ import annotations

from dataclasses import dataclass

REASONING_ORDER = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}

# Canonical display label for the persisted ``custom/best-coding`` Combo.
# The web UI's model picker surfaces this as the curated interactive
# execution combo. Rejecting this string as a route id (it is the
# label, not the wire id) is part of the canonical-route contract.
CUSTOM_BEST_CODING_DISPLAY_NAME = "OmniRoute Coding Best"

# Background-scoped Combo ids that must NOT be accepted as interactive
# execution routes. They remain available to direct callers (the M3-only
# Task Outcome evaluator, in particular) but are excluded from
# ``is_executable_route_id`` so the composer cannot route a user turn
# through them.
RESERVED_NON_EXECUTABLE_ROUTE_IDS: frozenset[str] = frozenset(
    {
        # The M3-only Task Outcome evaluator combo. The evaluator uses
        # ``custom/outcome-scoring`` directly; user-facing sessions must
        # never pick it.
        "custom/outcome-scoring",
    }
)

# Compatibility alias the OmniRoute wire surface can use when callers
# route through ``omniroute/<route_id>``. The persisted Combo ids on
# Hermes never carry this prefix; the prefix appears only when a caller
# concatenates ``omniroute`` (the transport) with the route id. We
# normalize it back to the canonical form so downstream code never has
# to think about it.
OMNIROUTE_TRANSPORT_PREFIX = "omniroute/"


@dataclass(frozen=True)
class OmniRouteProfile:
    route_id: str
    route_kind: str
    task_suitability: str
    quality_tier: str
    default_reasoning_effort: str
    max_reasoning_effort: str
    allowed_reasoning_efforts: tuple[str, ...]
    requires_explicit_approval: bool
    allow_subscription: bool
    allow_api_billed: bool
    allow_unknown_billing: bool
    requires_coding_capability: bool
    requires_reasoning_support: bool
    risk_note: str
    # Curated human label for the web UI's model picker. Defaults to
    # ``route_id`` when not provided so older catalogs keep rendering.
    display_name: str = ""


_PROFILES = [
    OmniRouteProfile(
        "auto",
        "auto_builtin",
        "general chat, routing, planning",
        "balanced",
        "medium",
        "high",
        ("low", "medium", "high"),
        False,
        True,
        False,
        False,
        False,
        False,
        "General automatic route; avoid for specialized hard coding.",
        "OmniRoute Auto",
    ),
    OmniRouteProfile(
        "auto/cheap",
        "auto_builtin",
        "routing, planning, simple chat",
        "economy",
        "low",
        "medium",
        ("low", "medium"),
        False,
        True,
        False,
        False,
        False,
        False,
        "Low-cost route; not for serious repo edits.",
        "OmniRoute Cheap",
    ),
    OmniRouteProfile(
        "auto/best-free",
        "auto_builtin",
        "simple tasks where free is adequate",
        "free",
        "low",
        "medium",
        ("low", "medium"),
        False,
        False,
        False,
        False,
        False,
        False,
        "Free route may trade quality/reliability for zero cost.",
        "OmniRoute Best Free",
    ),
    OmniRouteProfile(
        "auto/coding",
        "auto_builtin",
        "normal coding and repo edits",
        "standard",
        "medium",
        "high",
        ("low", "medium", "high"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Standard coding route.",
        "OmniRoute Coding",
    ),
    OmniRouteProfile(
        "auto/coding:fast",
        "auto_builtin",
        "quick coding fixes",
        "fast",
        "low",
        "medium",
        ("low", "medium"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Fast coding route; use for small changes.",
        "OmniRoute Coding Fast",
    ),
    OmniRouteProfile(
        "auto/coding:cheap",
        "auto_builtin",
        "light coding and review",
        "economy",
        "low",
        "medium",
        ("low", "medium"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Cheap coding route; avoid serious repo edits.",
        "OmniRoute Coding Cheap",
    ),
    OmniRouteProfile(
        "auto/coding:free",
        "auto_builtin",
        "light coding and review",
        "free",
        "low",
        "medium",
        ("low", "medium"),
        False,
        False,
        False,
        False,
        True,
        False,
        "Free coding route; use only when quality/reliability tradeoff is acceptable.",
        "OmniRoute Coding Free",
    ),
    OmniRouteProfile(
        "auto/coding:pro",
        "auto_builtin",
        "hard coding tasks",
        "pro",
        "high",
        "max",
        ("high", "xhigh", "max"),
        True,
        True,
        False,
        False,
        True,
        True,
        "Explicit approval required; may use pro/premium routing.",
        "OmniRoute Coding Pro",
    ),
    OmniRouteProfile(
        "auto/coding:reliable",
        "auto_builtin",
        "coding requiring reliability",
        "reliable",
        "medium",
        "high",
        ("medium", "high"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Prioritizes reliability over cost/speed.",
        "OmniRoute Coding Reliable",
    ),
    OmniRouteProfile(
        "auto/smart",
        "auto_builtin",
        "broad quality route",
        "quality",
        "medium",
        "high",
        ("medium", "high"),
        True,
        True,
        False,
        False,
        False,
        False,
        "Explicit approval recommended for broad quality routing.",
        "OmniRoute Smart",
    ),
    OmniRouteProfile(
        "auto/fast",
        "auto_builtin",
        "quick simple tasks",
        "fast",
        "low",
        "medium",
        ("low", "medium"),
        False,
        True,
        False,
        False,
        False,
        False,
        "Fast route; not for hard tasks.",
        "OmniRoute Fast",
    ),
    OmniRouteProfile(
        "auto/reasoning",
        "auto_builtin",
        "reasoning-heavy tasks",
        "reasoning",
        "medium",
        "high",
        ("medium", "high"),
        False,
        True,
        False,
        False,
        False,
        True,
        "Reasoning-capable pool; reasoning effort is still selected separately.",
        "OmniRoute Reasoning",
    ),
    OmniRouteProfile(
        "auto/reasoning:pro",
        "auto_builtin",
        "hardest reasoning tasks",
        "pro",
        "high",
        "max",
        ("high", "xhigh", "max"),
        True,
        True,
        False,
        False,
        False,
        True,
        "Explicit approval required; may use pro/premium reasoning routes.",
        "OmniRoute Reasoning Pro",
    ),
    OmniRouteProfile(
        "auto/vision",
        "auto_builtin",
        "vision inputs only",
        "multimodal",
        "medium",
        "high",
        ("medium", "high"),
        False,
        True,
        False,
        False,
        False,
        False,
        "Use only for image/vision input.",
        "OmniRoute Vision",
    ),
    OmniRouteProfile(
        "auto/multimodal",
        "auto_builtin",
        "multimodal inputs only",
        "multimodal",
        "medium",
        "high",
        ("medium", "high"),
        False,
        True,
        False,
        False,
        False,
        False,
        "Use only for multimodal input.",
        "OmniRoute Multimodal",
    ),
    # Curated "best coding" route: full quality tier with the same effort
    # range as ``auto/coding:reliable`` so the web UI's model picker can
    # surface it as the recommended coding combo. The id preserves its
    # slash verbatim so the runtime can round-trip it through
    # :func:`omnigent.opencode_native_provider.qualify_omniroute_model`.
    OmniRouteProfile(
        "auto/best-coding",
        "auto_builtin",
        "best coding, repo edits, and reviews",
        "quality",
        "medium",
        "high",
        ("medium", "high"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Best coding route; default quality tier for repo-grade work.",
        "OmniRoute Coding Best",
    ),
    # Persisted custom Combo ``custom/best-coding`` — the canonical
    # interactive execution combo used when the web UI selects
    # "OmniRoute Coding Best" via the live OmniRoute catalog. Lives
    # outside the ``auto/`` namespace by construction (the runtime's
    # auto-prefix parser only intercepts ``auto/*``). Defined in
    # ``omniroute-customizations/combos/custom-best-coding.json`` as
    # a persisted Combo with quality-first scoring; OmniRoute returns
    # it on ``/v1/models`` with ``owned_by="combo"`` so the picker
    # shows it as a routable option.
    OmniRouteProfile(
        "custom/best-coding",
        "custom_persisted",
        "best coding, repo edits, and reviews (custom persisted combo)",
        "quality",
        "low",
        "high",
        ("low", "medium", "high"),
        False,
        True,
        False,
        False,
        True,
        False,
        "Custom persisted best-coding combo; quality-first scoring with rules router.",
        "OmniRoute Coding Best",
    ),
]

OMNIROUTE_ROUTE_CATALOG: dict[str, OmniRouteProfile] = {p.route_id: p for p in _PROFILES}
NATIVE_OMNIROUTE_ROUTE_IDS: tuple[str, ...] = tuple(OMNIROUTE_ROUTE_CATALOG)


def get_route_profile(route_id: str) -> OmniRouteProfile | None:
    return OMNIROUTE_ROUTE_CATALOG.get(route_id)


def is_known_route_id(route_id: str) -> bool:
    """True iff *route_id* has a known OmniRoute profile.

    Includes built-in ``auto/*`` virtual Auto Combos and the persisted
    ``custom/best-coding`` Combo. Excludes ``custom/outcome-scoring``,
    display labels, and unknown values. Prefer
    :func:`is_executable_route_id` for interactive-execution gating.
    """
    return route_id in OMNIROUTE_ROUTE_CATALOG


def normalize_route_id(value: str | None) -> str | None:
    """Return the canonical wire id for *value*, or ``None`` if not normalizable.

    Accepts the bare canonical id (``custom/best-coding``) verbatim and
    strips the ``omniroute/`` transport prefix when a caller (test, log
    replay, or upstream serializer) has concatenated it. Strips
    surrounding whitespace. Does NOT rewrite display labels, does NOT
    fabricate a route id from arbitrary text. The canonical form is
    always the bare id so persistence, dispatch, and provenance
    headers stay byte-for-byte comparable.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    if candidate.startswith(OMNIROUTE_TRANSPORT_PREFIX):
        candidate = candidate[len(OMNIROUTE_TRANSPORT_PREFIX) :]
    return candidate or None


def is_executable_route_id(value: str | None) -> bool:
    """True iff *value* is an OmniRoute route id Omnigent accepts for execution.

    Rejects ``None``, the empty string, bare display labels, unknown
    ``custom/*`` routes, and the reserved background-only combos
    (``custom/outcome-scoring``). Accepts built-in ``auto/*`` virtual
    Auto Combos and the persisted ``custom/best-coding`` combo after
    transport-prefix normalization.
    """
    canonical = normalize_route_id(value)
    if canonical is None:
        return False
    if canonical in RESERVED_NON_EXECUTABLE_ROUTE_IDS:
        return False
    return canonical in OMNIROUTE_ROUTE_CATALOG


def executable_route_ids() -> tuple[str, ...]:
    """All OmniRoute route ids Omnigent accepts for interactive execution.

    Excludes ``RESERVED_NON_EXECUTABLE_ROUTE_IDS`` (background-only).
    """
    return tuple(
        sorted(
            route_id
            for route_id in OMNIROUTE_ROUTE_CATALOG
            if route_id not in RESERVED_NON_EXECUTABLE_ROUTE_IDS
        )
    )


def get_route_display_name(route_id: str) -> str:
    """Return a curated display label for *route_id*, falling back to the id.

    Used by the web UI's model picker when a chosen route is not present in
    the runtime catalog (so :func:`get_route_profile` returns ``None``).
    Preserves the raw id verbatim — never rewrites colons, slashes, or
    brackets — because the id is what the runner dispatches on.
    """
    profile = get_route_profile(route_id)
    if profile is not None and profile.display_name:
        return profile.display_name
    return route_id


def reasoning_lte(value: str, max_value: str) -> bool:
    return REASONING_ORDER.get(value, 999) <= REASONING_ORDER.get(max_value, -1)
