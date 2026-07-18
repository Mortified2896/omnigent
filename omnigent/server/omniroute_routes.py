"""Native OmniRoute route catalog used by Omnigent routing approval."""

from __future__ import annotations

from dataclasses import dataclass

REASONING_ORDER = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4, "xhigh": 5, "max": 6}


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
]

OMNIROUTE_ROUTE_CATALOG: dict[str, OmniRouteProfile] = {p.route_id: p for p in _PROFILES}
NATIVE_OMNIROUTE_ROUTE_IDS: tuple[str, ...] = tuple(OMNIROUTE_ROUTE_CATALOG)


def get_route_profile(route_id: str) -> OmniRouteProfile | None:
    return OMNIROUTE_ROUTE_CATALOG.get(route_id)


def is_known_route_id(route_id: str) -> bool:
    return route_id in OMNIROUTE_ROUTE_CATALOG


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
