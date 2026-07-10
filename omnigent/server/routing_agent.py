"""LLM-backed model routing agent for native OmniRoute routes.

Note on ``api_billed``
----------------------

``api_billed`` is an **existing billing classification** reported by
OmniRoute. It denotes metered / pay-per-use billing on the underlying
provider account. It is NOT a transport flag and it does NOT mean
"every provider reached through an API is forbidden".

API transport is orthogonal: API keys, OAuth, OpenAI-compatible
endpoints, local proxies, and subscription bridges exposed through an
API are all valid transport / authentication mechanisms. A free-tier
or subscription model served through any of those transports is still
classified as ``free`` or ``subscription`` by OmniRoute — never as
``api_billed`` — and remains fully eligible for Omnigent routing
according to the route profile.

The validator therefore never rejects, hides, or downgrades a route
solely because it uses an API; the only filter is the OmniRoute-
reported billing class, which is treated as factual provenance here.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from dataclasses import asdict, dataclass, fields
from typing import Any, Literal

import httpx

from omnigent.reasoning_effort import EFFORT_VALUES, validate_effort
from omnigent.server.omniroute_routes import (
    NATIVE_OMNIROUTE_ROUTE_IDS,
    OMNIROUTE_ROUTE_CATALOG,
    get_route_profile,
    reasoning_lte,
)

_logger = logging.getLogger(__name__)


# ── Routing-input budget ───────────────────────────────────────────────
#
# The routing LLM only needs a bounded excerpt of the user message to
# pick a route + permission mode. Sending the full long prompt:
# - bloats every routing call (slow on small context-window judges)
# - can blow past the evaluator's timeout on slow backends
# - can confuse the judge on what really matters (the head + tail of
#   the message is usually enough)
#
# ``OMNIGENT_ROUTER_INPUT_BUDGET_CHARS`` controls the max length of the
# routing-only excerpt (the user-message slice embedded into the
# prompt). Default 4000, matching the pre-existing
# ``_extract_user_text_for_routing`` audit slice. The execution path
# always forwards the FULL original user message to the runner — the
# budget only affects the routing judge.
DEFAULT_ROUTING_INPUT_BUDGET_CHARS = 4000
_OMISSION_MARKER_TEMPLATE = (
    "\n\n... [{omitted_chars} characters omitted from routing-only "
    "representation; original user message has {original_chars} chars] ...\n\n"
)


def routing_input_budget_chars() -> int:
    """Return the configured routing-input character budget.

    Reads ``OMNIGENT_ROUTER_INPUT_BUDGET_CHARS`` on every call so a
    server restart isn't required to pick up a new value. Values
    below 200 are clamped to 200 (need enough headroom for at least
    a short greeting + omission marker).
    """
    raw = os.environ.get("OMNIGENT_ROUTER_INPUT_BUDGET_CHARS", "").strip()
    if not raw:
        return DEFAULT_ROUTING_INPUT_BUDGET_CHARS
    try:
        value = int(raw)
    except ValueError:
        _logger.warning(
            "routing_input_budget: OMNIGENT_ROUTER_INPUT_BUDGET_CHARS=%r is not an int; "
            "using default %d",
            raw,
            DEFAULT_ROUTING_INPUT_BUDGET_CHARS,
        )
        return DEFAULT_ROUTING_INPUT_BUDGET_CHARS
    if value < 200:
        _logger.warning(
            "routing_input_budget: OMNIGENT_ROUTER_INPUT_BUDGET_CHARS=%d is below the "
            "200-char minimum; clamping to 200",
            value,
        )
        return 200
    return value


def _bound_routing_user_message(user_message: str, budget: int) -> str:
    """Return a deterministic, budget-bounded routing-only excerpt.

    Short messages (≤ *budget* chars) pass through unchanged. Long
    messages are compacted to preserve the head and tail with an
    explicit omission marker that names the original size, so the
    routing LLM knows the message is bigger than what it sees and
    can act accordingly (e.g. flag a coding-vs-reasoning decision
    that hinges on the middle, or downgrade effort for a long
    context-bound task).

    The boundary cuts are codepoint-safe — they never split a
    multi-byte UTF-8 character in half. Code-block fences (`` ``` ``)
    and backtick runs are also kept intact by preferring to break
    at whitespace, so the router never sees a broken ```` ``` `` fence
    that could trigger a Markdown rendering bug in the routing
    LLM's own output.
    """
    if not isinstance(user_message, str):
        user_message = "" if user_message is None else str(user_message)
    if len(user_message) <= budget:
        return user_message
    original_chars = len(user_message)
    # Split the budget roughly in half, leaving a few chars slack for
    # the omission marker when re-joined.
    head_budget = budget // 2
    tail_budget = budget - head_budget
    head = _safe_truncate_to_boundary(user_message, head_budget, side="left")
    tail = _safe_truncate_to_boundary(user_message, tail_budget, side="right")
    omitted_chars = max(0, original_chars - len(head) - len(tail))
    marker = _OMISSION_MARKER_TEMPLATE.format(
        omitted_chars=omitted_chars, original_chars=original_chars
    )
    return f"{head}{marker}{tail}"


def _safe_truncate_to_boundary(text: str, limit: int, *, side: str) -> str:
    """Truncate *text* to ≤ *limit* chars without splitting a codepoint.

    *side* ``"left"`` keeps the first *limit* chars (head); ``"right"``
    keeps the last *limit* chars (tail). Prefers to break at the
    last whitespace before the limit so the routing LLM does not see
    a half-word boundary.
    """
    if limit <= 0 or len(text) <= limit:
        return text
    if side == "left":
        candidate = text[:limit]
        # Snap back to the last whitespace so we don't end a token mid-word.
        last_ws = max(candidate.rfind(" "), candidate.rfind("\n"), candidate.rfind("\t"))
        if last_ws > limit - max(40, limit // 4):
            candidate = candidate[:last_ws]
        return candidate
    # side == "right" → keep the tail.
    candidate = text[-limit:]
    first_ws = min(
        (i for i, ch in enumerate(candidate) if ch in " \n\t"),
        default=-1,
    )
    if first_ws != -1 and first_ws < limit // 4:
        candidate = candidate[first_ws + 1 :]
    return candidate


def hash_user_message(user_message: str) -> str:
    """Return the SHA-256 prefix (12 hex chars) of the full user message.

    Used as the ``source_input_sha256_prefix`` on
    :class:`RouteProposal` so stale-approval checks compare against the
    COMPLETE original prompt, not the bounded routing-only excerpt.
    """
    return hashlib.sha256(user_message.encode("utf-8")).hexdigest()[:12]


KNOWN_PERMISSION_MODES = frozenset(
    {
        "ask_before_edits",
        "ask_before_commands",
        "read_only",
        "auto_accept_edits",
        "bypass",
    }
)
KNOWN_HARNESSES = frozenset(
    {
        "opencode-native",
        "open-code-native",
        "OpenCode Native",
        "claude-native",
        "codex-native",
        "pi",
    }
)
_KNOWN_BILLING_CLASSES = frozenset({"free", "subscription", "api_billed", "unknown"})
_KNOWN_FALLBACK_POLICIES = frozenset(
    {
        "fail_closed_no_api_billed_fallback",
        "allow_any_fallback",
        "fail_closed",
    }
)
_DEFAULT_ROUTE_POLICY_SOURCE = "default_route_policy"
_ROUTER_PROMPT_PERMISSION_RULES = (
    "Permission mode is a safety policy, not a model-quality setting. "
    "Pick the strictest mode that still lets the task complete. "
    "Use 'read_only' for greetings, general chat, explanations, plan-only "
    "requests, review-only requests, and any prompt that says 'do not edit "
    "files', 'do not run tools', 'no changes', or 'explain only'. "
    "Use 'ask_before_edits' ONLY when the user asks to create, modify, "
    "patch, refactor, delete, or commit files. "
    "Use 'ask_before_commands' ONLY when the user asks to run tests, shell "
    "commands, package installs, services, migrations, or other terminal "
    "actions. "
    "Never pick 'ask_before_edits' for a greeting, simple question, "
    "explanation, or plan-only request. "
    "Never pick 'ask_before_commands' unless command/tool execution is "
    "actually requested or necessary. "
    "Never pick 'auto_accept_edits' or 'bypass'."
)
_ROUTER_PROMPT_RULES = (
    "Rules: choose only provided route IDs; do not invent IDs. "
    "Use auto/cheap only for routing/planning/simple chat. "
    "Use auto/coding:cheap or auto/coding:free for light coding. "
    "Use auto/coding for normal coding. "
    "Use auto/coding:pro only for hard coding. "
    "Use auto/reasoning for reasoning-heavy tasks. "
    "Use auto/reasoning:pro only for hardest tasks. "
    "Use auto/vision or auto/multimodal only for multimodal input. "
    "Pick reasoning_effort separately from route ID. "
    f"{_ROUTER_PROMPT_PERMISSION_RULES} "
    "api_billed and unknown billing are forbidden unless explicitly allowed. "
    "Note: 'api_billed' is a metered-billing class (provider charges per "
    "token / per request). It is NOT a transport flag — providers reached "
    "through API keys, OAuth, OpenAI-compatible endpoints, local proxies, "
    "or subscription bridges are still eligible; they are simply reported "
    "by OmniRoute as free / subscription when the meter is not metered. "
    "Do not infer billing from the word 'api' in provider or model IDs. "
    "Use subscriptions when quality matters. "
    "Prefer free only when equivalent."
)
_ROUTER_PROMPT_FIELDS = (
    "Required JSON fields: task_type,recommended_harness,omniroute_route_id,"
    "reasoning_effort,permission_mode,allowed_billing_classes,forbidden_billing_classes,"
    "execution_fallback_policy,omniroute_requires_explicit_approval,rationale "
    "(array of strings)."
)
# Few-shot examples. Order matters: the LLM should see the read-only cases
# first so it learns to default to the strictest mode that still works.
_ROUTER_PROMPT_EXAMPLES: tuple[str, ...] = (
    # 1) Greeting -> cheapest route + read_only.
    '{"task_type":"general_chat","recommended_harness":"OpenCode Native",'
    '"omniroute_route_id":"auto/cheap","reasoning_effort":"low",'
    '"permission_mode":"read_only",'
    '"allowed_billing_classes":["free","subscription"],'
    '"forbidden_billing_classes":["api_billed","unknown"],'
    '"execution_fallback_policy":"fail_closed_no_api_billed_fallback",'
    '"omniroute_requires_explicit_approval":false,'
    '"rationale":["Trivial greeting; cheapest route and read-only mode suffice."]}',
    # 2) Plan-only with explicit "do not edit / do not run tools" -> read_only.
    '{"task_type":"general_chat","recommended_harness":"OpenCode Native",'
    '"omniroute_route_id":"auto/cheap","reasoning_effort":"low",'
    '"permission_mode":"read_only",'
    '"allowed_billing_classes":["free","subscription"],'
    '"forbidden_billing_classes":["api_billed","unknown"],'
    '"execution_fallback_policy":"fail_closed_no_api_billed_fallback",'
    '"omniroute_requires_explicit_approval":false,'
    '"rationale":["Plan-only request; user said do not edit or run tools."]}',
    # 3) Implement / repo edit -> ask_before_edits.
    '{"task_type":"coding","recommended_harness":"OpenCode Native",'
    '"omniroute_route_id":"auto/coding","reasoning_effort":"medium",'
    '"permission_mode":"ask_before_edits",'
    '"allowed_billing_classes":["free","subscription"],'
    '"forbidden_billing_classes":["api_billed","unknown"],'
    '"execution_fallback_policy":"fail_closed_no_api_billed_fallback",'
    '"omniroute_requires_explicit_approval":false,'
    '"rationale":["User asked to implement a fix; repo edits required."]}',
    # 4) Run tests / shell -> ask_before_commands.
    '{"task_type":"coding","recommended_harness":"OpenCode Native",'
    '"omniroute_route_id":"auto/coding","reasoning_effort":"medium",'
    '"permission_mode":"ask_before_commands",'
    '"allowed_billing_classes":["free","subscription"],'
    '"forbidden_billing_classes":["api_billed","unknown"],'
    '"execution_fallback_policy":"fail_closed_no_api_billed_fallback",'
    '"omniroute_requires_explicit_approval":false,'
    '"rationale":["User asked to run tests; shell command execution required."]}',
)
# Backwards-compatible alias for tests and external callers.
_ROUTER_PROMPT_EXAMPLE = _ROUTER_PROMPT_EXAMPLES[0]


def _build_proposal_json_schema() -> dict[str, object]:
    """Strict JSON schema for :class:`RouteProposal` payloads.

    Used when the routing agent calls a JSON-schema-capable LLM
    (e.g. the server-level :class:`PolicyLLMClient`); the same
    schema is reused by the env-var ``OMNIGENT_ROUTER_*`` direct
    httpx path so the LLM output is constrained to a single object
    type. Mirrors the field list in :data:`_ROUTER_PROMPT_FIELDS`.
    """
    route_enum = sorted(NATIVE_OMNIROUTE_ROUTE_IDS)
    return {
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": (
                    "Short category like 'coding', 'general_chat', 'reasoning', 'multimodal'."
                ),
            },
            "recommended_harness": {
                "type": "string",
                "enum": sorted(KNOWN_HARNESSES),
            },
            "omniroute_route_id": {
                "type": "string",
                "enum": route_enum,
            },
            "reasoning_effort": {
                "type": "string",
                "enum": sorted(EFFORT_VALUES),
            },
            "permission_mode": {
                "type": "string",
                "enum": sorted(KNOWN_PERMISSION_MODES),
            },
            "allowed_billing_classes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_KNOWN_BILLING_CLASSES)},
            },
            "forbidden_billing_classes": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(_KNOWN_BILLING_CLASSES)},
            },
            "execution_fallback_policy": {
                "type": "string",
                "enum": sorted(_KNOWN_FALLBACK_POLICIES),
            },
            "omniroute_requires_explicit_approval": {"type": "boolean"},
            "rationale": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
            },
        },
        "required": [
            "task_type",
            "recommended_harness",
            "omniroute_route_id",
            "reasoning_effort",
            "permission_mode",
            "allowed_billing_classes",
            "forbidden_billing_classes",
            "execution_fallback_policy",
            "omniroute_requires_explicit_approval",
            "rationale",
        ],
        "additionalProperties": False,
    }


PROPOSAL_JSON_SCHEMA: dict[str, object] = _build_proposal_json_schema()
ROUTER_PROMPT_VERSION = "route-proposal-v1"


@dataclass(frozen=True)
class RouteProposal:
    task_type: str
    recommended_harness: str
    omniroute_route_id: str
    reasoning_effort: str
    permission_mode: str
    allowed_billing_classes: list[str]
    forbidden_billing_classes: list[str]
    execution_fallback_policy: str
    omniroute_requires_explicit_approval: bool
    rationale: list[str]
    router_invoked: bool
    router_fallback_used: bool
    proposal_source: Literal["llm_router", "default_route_policy"]
    proposal_source_label: str
    router_evaluator_route: str | None = None
    actual_evaluator_model: str | None = None
    actual_evaluator_provider: str | None = None
    evaluator_billing_class: str | None = None
    evaluator_fallback_used: bool | None = None
    evaluator_fallback_model: str | None = None
    evaluator_selection_strategy: str | None = None
    evaluator_decision_id: str | None = None
    router_prompt_version: str = ROUTER_PROMPT_VERSION
    proposal_id: str | None = None
    source_input_sha256_prefix: str | None = None
    source_extracted_chars: int | None = None
    source_content_blocks: list[str] | None = None


@dataclass(frozen=True)
class RouteAlternative:
    omniroute_route_id: str
    reasoning_effort: str
    rationale: str


class RoutingAgentError(RuntimeError):
    pass


class EvaluatorProvenanceError(RoutingAgentError):
    pass


_ALLOWED_EVALUATOR_BILLING_CLASSES = {"free", "subscription"}
_FORBIDDEN_EVALUATOR_BILLING_CLASSES = {"api_billed", "unknown"}


def _is_auto_route(model: str | None) -> bool:
    value = (model or "").strip().lower()
    return value == "auto" or value.startswith("auto/")


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _get_path(obj: Any, path: tuple[str, ...]) -> Any:
    cur = obj
    for key in path:
        if not isinstance(cur, dict):
            return None
        cur = cur.get(key)
    return cur


def _parse_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return None


def _omniroute_metadata_from_payload(payload: dict[str, Any]) -> dict[str, Any]:
    candidates = [
        payload.get("omniroute"),
        payload.get("metadata"),
        _get_path(payload, ("metadata", "omniroute")),
    ]
    choices = payload.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        candidates.extend(
            [
                _get_path(first, ("omniroute",)),
                _get_path(first, ("message", "omniroute")),
                _get_path(first, ("message", "metadata")),
                _get_path(first, ("message", "metadata", "omniroute")),
            ]
        )
    merged: dict[str, Any] = {}
    for candidate in candidates:
        if isinstance(candidate, dict):
            merged.update(candidate)
    return merged


def extract_evaluator_provenance(
    *,
    requested_model: str,
    payload: dict[str, Any],
    headers: httpx.Headers | dict[str, str] | None = None,
    fallback_model: str | None = None,
    outer_fallback_used: bool = False,
) -> dict[str, Any]:
    meta = _omniroute_metadata_from_payload(payload)
    header_get = (headers or {}).get
    selected_model = _first_str(
        meta.get("selected_model"),
        meta.get("actual_provider_model"),
        meta.get("provider_model"),
        meta.get("routed_model"),
        header_get("x-omniroute-selected-model"),
        payload.get("actual_provider_model"),
        payload.get("selected_model"),
        payload.get("provider_model"),
        payload.get("routed_model"),
    )
    requested = _first_str(
        meta.get("requested_model"),
        meta.get("requested_route"),
        header_get("x-omniroute-requested-model"),
        requested_model,
    )
    if not selected_model or selected_model == requested_model or _is_auto_route(selected_model):
        raise EvaluatorProvenanceError(
            "OmniRoute evaluator provenance unavailable: concrete selected model is unknown"
        )
    provider = _first_str(
        meta.get("selected_provider"),
        meta.get("provider"),
        header_get("x-omniroute-selected-provider"),
    )
    if provider is None and "/" in selected_model:
        provider = selected_model.split("/", 1)[0]
    if provider is None:
        raise EvaluatorProvenanceError(
            "OmniRoute evaluator provenance unavailable: selected provider is unknown"
        )
    billing = _first_str(
        meta.get("selected_billing_class"),
        meta.get("billing_class"),
        meta.get("billing"),
        header_get("x-omniroute-billing-class"),
        header_get("x-omniroute-selected-billing-class"),
    )
    if billing not in _ALLOWED_EVALUATOR_BILLING_CLASSES:
        if billing in _FORBIDDEN_EVALUATOR_BILLING_CLASSES:
            raise EvaluatorProvenanceError(
                f"OmniRoute evaluator billing class {billing!r} is forbidden"
            )
        raise EvaluatorProvenanceError(
            "OmniRoute evaluator provenance unavailable: billing class is unknown"
        )
    fallback_value = (
        meta.get("fallback_used")
        if "fallback_used" in meta
        else header_get("x-omniroute-fallback-used")
    )
    fallback_used = _parse_bool(fallback_value)
    if fallback_used is None:
        fallback_used = outer_fallback_used
    return {
        "router_evaluator_route": requested or requested_model,
        "actual_evaluator_model": selected_model,
        "actual_evaluator_provider": provider,
        "evaluator_billing_class": billing,
        "evaluator_fallback_used": fallback_used,
        "evaluator_fallback_model": _first_str(
            meta.get("fallback_model"), header_get("x-omniroute-fallback-model"), fallback_model
        ),
        "evaluator_selection_strategy": _first_str(
            meta.get("selection_strategy"),
            meta.get("selected_route_template"),
            header_get("x-omniroute-selection-strategy"),
        ),
        "evaluator_decision_id": _first_str(
            meta.get("decision_id"), header_get("x-omniroute-decision-id")
        ),
        "router_prompt_version": ROUTER_PROMPT_VERSION,
    }


def validate_evaluator_auditability(proposal: RouteProposal) -> None:
    if proposal.proposal_source != "llm_router" or not proposal.router_invoked:
        return
    if not any(
        [
            proposal.router_evaluator_route,
            proposal.actual_evaluator_model,
            proposal.actual_evaluator_provider,
            proposal.evaluator_billing_class,
        ]
    ):
        return
    if not proposal.router_evaluator_route:
        raise EvaluatorProvenanceError("router evaluator route is missing")
    if not proposal.actual_evaluator_model or _is_auto_route(proposal.actual_evaluator_model):
        raise EvaluatorProvenanceError("concrete evaluator model is missing")
    if not proposal.actual_evaluator_provider:
        raise EvaluatorProvenanceError("concrete evaluator provider is missing")
    if proposal.evaluator_billing_class not in _ALLOWED_EVALUATOR_BILLING_CLASSES:
        raise EvaluatorProvenanceError("allowed evaluator billing provenance is missing")


def route_approval_gate_enabled() -> bool:
    return os.environ.get("OMNIGENT_ROUTE_APPROVAL_GATE", "true").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def fail_open_to_default_policy_enabled() -> bool:
    """Whether the agent falls back to a static default policy on error.

    Disabled by default — fail-closed surfaces a clear user-facing
    error. ``OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true``
    (or ``1|yes|on``) opts in. Used only as an emergency escape
    hatch; the default policy is a static hardcoded ``auto/coding``
    proposal and is NOT a router recommendation.
    """
    return os.environ.get("OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def validate_route_proposal(proposal: RouteProposal) -> RouteProposal:
    if proposal.proposal_source == "llm_router" and not proposal.router_invoked:
        raise RoutingAgentError("llm_router proposal must have router_invoked=true")
    profile = get_route_profile(proposal.omniroute_route_id)
    if profile is None:
        raise RoutingAgentError(f"unknown OmniRoute route id: {proposal.omniroute_route_id}")
    if proposal.recommended_harness not in KNOWN_HARNESSES:
        raise RoutingAgentError(f"unknown harness: {proposal.recommended_harness}")
    try:
        effort = validate_effort(proposal.reasoning_effort, "route proposal", EFFORT_VALUES)
    except ValueError as exc:
        raise RoutingAgentError(str(exc)) from exc
    if effort not in profile.allowed_reasoning_efforts:
        raise RoutingAgentError(f"reasoning effort {effort!r} not allowed for {profile.route_id}")
    if not reasoning_lte(effort, profile.max_reasoning_effort):
        raise RoutingAgentError(f"reasoning effort {effort!r} exceeds max for {profile.route_id}")
    if proposal.permission_mode not in KNOWN_PERMISSION_MODES:
        raise RoutingAgentError(f"unknown permission mode: {proposal.permission_mode}")
    allowed = set(proposal.allowed_billing_classes)
    if "api_billed" in allowed and not profile.allow_api_billed:
        raise RoutingAgentError(f"api_billed forbidden for {profile.route_id}")
    if "unknown" in allowed and not profile.allow_unknown_billing:
        raise RoutingAgentError(f"unknown billing forbidden for {profile.route_id}")
    if not proposal.rationale:
        raise RoutingAgentError("missing rationale")
    requires = profile.requires_explicit_approval or proposal.omniroute_route_id.endswith(":pro")
    if requires and not proposal.omniroute_requires_explicit_approval:
        proposal = RouteProposal(
            **{**asdict(proposal), "omniroute_requires_explicit_approval": True}
        )
    validate_evaluator_auditability(proposal)
    return proposal


# Lightweight keyword cues used by the permission floor to down-grade too-strong
# proposals. The sets are intentionally small: the goal is to catch obvious
# over-permissioning (e.g. greeting => ask_before_edits), not to fully parse
# natural language. False negatives are tolerable; the upstream router prompt
# carries the real semantics. False positives are bounded — we only ever
# DOWN-grade, never UP-grade.
_READ_ONLY_CUES: tuple[str, ...] = (
    "do not edit",
    "don't edit",
    "do not run tools",
    "don't run tools",
    "no tools",
    "no edits",
    "no changes",
    "no file changes",
    "plan only",
    "plan how",
    "explain only",
    "explain what",
    "explain how",
    "review only",
    "read only",
    "read-only",
    "just describe",
    "without modifying",
    "without changing",
    "without running",
    "without editing",
    "without making changes",
    "what would you do",
    "what you would do",
)
_EDIT_CUES: tuple[str, ...] = (
    "implement",
    "write a",
    "write the",
    "add a",
    "add the",
    "create a",
    "create the",
    "modify",
    "refactor",
    "patch",
    "delete the",
    "delete file",
    "remove the",
    "rename the",
    "fix the",
    "fix bug",
    "fix issue",
    "commit",
    "edit file",
    "edit the file",
    "update the file",
    "rewrite",
)
_COMMAND_CUES: tuple[str, ...] = (
    "run the tests",
    "run tests",
    "run test",
    "run pytest",
    "run npm test",
    "run npm",
    "run yarn",
    "run pnpm",
    "run cargo",
    "run go test",
    "run make",
    "run pytest",
    "run the suite",
    "run shell",
    "run a shell",
    "shell command",
    "execute the command",
    "install deps",
    "install the",
    "npm install",
    "pip install",
    "yarn install",
    "pnpm install",
    "brew install",
    "apt install",
    "run migrations",
    "start the server",
    "restart the server",
    "kill the process",
    "docker run",
    "docker compose",
    "kubectl",
    "terraform",
    "deploy",
)


def _message_signals(user_message: str) -> tuple[bool, bool, bool]:
    """Classify a user message into (read_only_eligible, edit_request, command_request).

    The classifier is intentionally conservative. It only flags READ-ONLY cues
    or COMMAND cues positively when the literal phrase is present; edit cues
    are checked last and require a recognised verb so we don't accidentally
    flag a question like "can you explain how to refactor this?".
    """
    text = (user_message or "").lower()
    if not text.strip():
        # Empty prompt — treat as read-only eligible. No edits, no commands.
        return True, False, False
    read_only = any(cue in text for cue in _READ_ONLY_CUES)
    command = any(cue in text for cue in _COMMAND_CUES)
    # Only treat as an edit request when the cue is present AND the message
    # does NOT also carry an explicit read-only signal. A "plan how to
    # refactor this" prompt is still plan-only.
    edit = (not read_only) and any(cue in text for cue in _EDIT_CUES)
    return read_only, edit, command


# Order matters: index 0 is the strictest mode. A downgrade is any move toward
# index 0; an upgrade is any move away from index 0. The guard only downgrades.
_PERMISSION_STRICTNESS: tuple[str, ...] = (
    "read_only",
    "ask_before_edits",
    "ask_before_commands",
    "auto_accept_edits",
    "bypass",
)


def _permission_floor(user_message: str) -> str:
    """Return the strictest permission mode acceptable for the user message.

    - read-only-eligible messages => ``read_only``
    - command request (no explicit read-only) => ``ask_before_commands``
    - edit request (no command, no read-only) => ``ask_before_edits``
    - otherwise => ``read_only`` (safest default; never silently grants edits)
    """
    read_only, edit, command = _message_signals(user_message)
    if read_only:
        return "read_only"
    if command:
        return "ask_before_commands"
    if edit:
        return "ask_before_edits"
    # Plain chat, greeting, or anything without edit/command intent =>
    # the safest mode is read_only. This is the key behaviour: a vague or
    # trivial prompt must not receive edit/command authority.
    return "read_only"


def _route_proposal_from_data(data: dict[str, Any]) -> RouteProposal:
    """Build a :class:`RouteProposal` from a parsed router response dict.

    Filters unknown keys defensively so an LLM that emits extra fields
    (e.g. ``"command"``) cannot blow up the frozen dataclass with a
    :class:`TypeError: unexpected keyword argument`. Unknown keys are
    logged at DEBUG — the validator still raises if a REQUIRED field is
    missing or invalid; this is purely a robustness guard.
    """
    allowed = {f.name for f in fields(RouteProposal)}
    unknown = sorted(k for k in data if k not in allowed)
    if unknown:
        _logger.debug(
            "routing_agent: stripping %d unknown keys from router response: %s",
            len(unknown),
            unknown,
        )
        filtered = {k: v for k, v in data.items() if k in allowed}
    else:
        filtered = data
    return RouteProposal(**filtered)


def _enforce_permission_floor(proposal: RouteProposal, user_message: str) -> RouteProposal:
    """Down-grade a too-strong permission_mode on a validated router proposal.

    The router can over-permission trivial prompts (e.g. ``ask_before_edits``
    for a greeting). This guard is the safety net:

    - Only ever moves ``permission_mode`` toward ``read_only`` (never upgrades).
    - Appends a ``Permission floor: ...`` rationale entry so the user-facing
      card can show why the proposal was adjusted.
    - Leaves edit / shell proposals unchanged when the message signals an
      edit or shell intent.
    - Returns the input proposal unchanged when no downgrade is needed.
    """
    current = proposal.permission_mode
    if current not in _PERMISSION_STRICTNESS:
        # Unknown / new mode: leave alone. validate_route_proposal already
        # raised if it was truly unknown; this is a defensive fallback.
        return proposal
    floor = _permission_floor(user_message)
    current_idx = _PERMISSION_STRICTNESS.index(current)
    floor_idx = _PERMISSION_STRICTNESS.index(floor)
    if current_idx <= floor_idx:
        # Already at or stricter than the floor — no change.
        return proposal
    adjusted = floor
    note = (
        f"Permission floor: router proposed '{current}' but user message is "
        f"read-only-eligible (signals: read_only={_message_signals(user_message)[0]}, "
        f"command={_message_signals(user_message)[2]}). Downgrading to '{adjusted}'."
        if adjusted == "read_only"
        else (
            f"Permission floor: router proposed '{current}' but user message "
            f"signals command execution (signals: command=True). Downgrading to "
            f"'{adjusted}'."
            if adjusted == "ask_before_commands"
            else (
                f"Permission floor: router proposed '{current}' but user message "
                f"does not signal edit intent. Downgrading to '{adjusted}'."
            )
        )
    )
    _logger.info(
        "model_routing_agent permission_floor session=N/A from=%s to=%s note=%s",
        current,
        adjusted,
        note,
    )
    return RouteProposal(
        **{
            **asdict(proposal),
            "permission_mode": adjusted,
            "rationale": [*proposal.rationale, note],
        }
    )


class RoutingAgent:
    """Model Routing Agent — picks a native OmniRoute for an inbound user message.

    Resolution order:

    1. **Server-level LLM client** (``policy_llm_client``): the same
       :class:`PolicyLLMClient` used for policy functions / smart
       routing. Preferred path on this machine since it inherits
       the existing ``llm:`` server config wiring.
    2. **Direct httpx** using the ``OMNIGENT_ROUTER_*`` env vars.
       Useful when the routing agent should target an explicit
       endpoint (e.g. a smaller / cheaper model split off from
       the main policy LLM).
    3. **Fail-closed** by default: raises :class:`RoutingAgentError`
       with a clear ``"unavailable; fail-closed"`` message. Set
       ``OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true`` to
       opt back into the emergency default policy (NOT a router
       recommendation — labelled ``"Router unavailable — default
       policy used"``).
    """

    def __init__(
        self,
        *,
        primary_model: str | None = None,
        fallback_model: str | None = None,
        api_url: str | None = None,
        api_key: str | None = None,
        timeout: float = 20.0,
        policy_llm_client: Any | None = None,
    ) -> None:
        self.primary_model = primary_model or os.environ.get("OMNIGENT_ROUTER_MODEL")
        self.fallback_model = fallback_model or os.environ.get("OMNIGENT_ROUTER_FALLBACK_MODEL")
        self.api_url = api_url or os.environ.get("OMNIGENT_ROUTER_API_URL")
        self.api_key = api_key or os.environ.get("OMNIGENT_ROUTER_API_KEY")
        self.timeout = timeout
        self.policy_llm_client = policy_llm_client

    async def propose(
        self,
        *,
        user_message: str,
        available_harnesses: list[str] | None = None,
        billing_policy: str = (
            "free and subscription allowed; "
            "api_billed (metered billing) and unknown forbidden unless "
            "explicitly allowed. api_billed is NOT a transport flag — API "
            "keys, OAuth, OpenAI-compatible endpoints, and local proxies are "
            "transport/authentication mechanisms, not reasons to reject a "
            "provider. Do not infer api_billed from the word 'api' in "
            "provider or model IDs."
        ),
        routing_user_message: str | None = None,
    ) -> RouteProposal:
        """Pick an :class:`OmniRoute` route for *user_message*.

        Two distinct values flow through this method:

        - ``user_message`` — the COMPLETE original prompt, forwarded
          to the coding harness by the caller. The routing agent does
          not forward anything; it only classifies. Surfaced via
          :attr:`RouteProposal.source_extracted_chars` /
          ``source_input_sha256_prefix`` for audit and stale-approval
          checks.
        - ``routing_user_message`` — the bounded excerpt the routing
          LLM actually sees. When ``None``, the agent derives it via
          :func:`_bound_routing_user_message` using
          :func:`routing_input_budget_chars`. Callers that already
          pre-truncated (e.g. the session-event dispatcher's
          ``_extract_user_text_for_routing`` audit slice) can pass
          that slice verbatim here.

        The execution path always receives the FULL original prompt;
        the bounding is strictly a routing-input optimisation.
        """
        budget = routing_input_budget_chars()
        if routing_user_message is None:
            routing_user_message = _bound_routing_user_message(user_message, budget)
        errors: list[str] = []
        if self.policy_llm_client is not None:
            try:
                return await self._call_via_policy_llm(
                    user_message,
                    routing_user_message,
                    available_harnesses,
                    billing_policy,
                    False,
                )
            except EvaluatorProvenanceError:
                raise
            except (
                httpx.HTTPError,
                RoutingAgentError,
                ValueError,
                OSError,
                AttributeError,
            ) as exc:
                errors.append(f"policy_llm: {exc}")
        if self.primary_model and self.api_url:
            try:
                return await self._call_and_validate(
                    self.primary_model,
                    user_message,
                    routing_user_message,
                    available_harnesses,
                    billing_policy,
                    False,
                )
            except EvaluatorProvenanceError:
                raise
            except (httpx.HTTPError, RoutingAgentError, ValueError, OSError) as exc:
                errors.append(f"primary: {exc}")
        if self.fallback_model and self.api_url:
            try:
                return await self._call_and_validate(
                    self.fallback_model,
                    user_message,
                    routing_user_message,
                    available_harnesses,
                    billing_policy,
                    True,
                )
            except EvaluatorProvenanceError:
                raise
            except (httpx.HTTPError, RoutingAgentError, ValueError, OSError) as exc:
                errors.append(f"fallback: {exc}")
        if fail_open_to_default_policy_enabled():
            _logger.warning(
                "RoutingAgent: no router reachable; falling back to default policy "
                "(OMNIGENT_ROUTER_FAIL_OPEN_TO_DEFAULT_POLICY=true)"
            )
            return validate_route_proposal(
                RouteProposal(
                    task_type="coding",
                    recommended_harness="OpenCode Native",
                    omniroute_route_id="auto/coding",
                    reasoning_effort="medium",
                    permission_mode="ask_before_edits",
                    allowed_billing_classes=["free", "subscription"],
                    forbidden_billing_classes=["api_billed", "unknown"],
                    execution_fallback_policy="fail_closed_no_api_billed_fallback",
                    omniroute_requires_explicit_approval=False,
                    rationale=["Router unavailable — default policy used"],
                    router_invoked=False,
                    router_fallback_used=False,
                    proposal_source="default_route_policy",
                    proposal_source_label="Router unavailable — default policy used",
                    source_input_sha256_prefix=hash_user_message(user_message),
                    source_extracted_chars=len(user_message),
                )
            )
        raise RoutingAgentError(
            "Model Routing Agent unavailable; fail-closed"
            + (": " + "; ".join(errors) if errors else "")
        )

    async def _call_and_validate(
        self,
        model: str,
        user_message: str,
        routing_user_message: str,
        available_harnesses: list[str] | None,
        billing_policy: str,
        fallback: bool,
    ) -> RouteProposal:
        raw, provenance = await self._invoke_router(
            model,
            self._prompt(routing_user_message, available_harnesses, billing_policy),
            fallback=fallback,
        )
        data = self._parse_router_response(raw)
        data.update(provenance)
        data.setdefault("router_invoked", True)
        data.setdefault("router_fallback_used", fallback)
        data.setdefault("proposal_source", "llm_router")
        data.setdefault("proposal_source_label", "Router recommendation")
        data.setdefault("source_input_sha256_prefix", hash_user_message(user_message))
        data.setdefault("source_extracted_chars", len(user_message))
        proposal = _route_proposal_from_data(data)
        proposal = validate_route_proposal(proposal)
        return _enforce_permission_floor(proposal, user_message)

    async def _call_via_policy_llm(
        self,
        user_message: str,
        routing_user_message: str,
        available_harnesses: list[str] | None,
        billing_policy: str,
        fallback: bool,
    ) -> RouteProposal:
        """Call the server-level :class:`PolicyLLMClient` for a JSON proposal."""
        prompt = self._prompt(routing_user_message, available_harnesses, billing_policy)
        response = await self.policy_llm_client.create(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
            instructions=(
                "You are Omnigent's Model Routing Agent. Return strict JSON matching "
                "the provided schema only; never add markdown, prose, or extra keys."
            ),
            text={
                "format": {
                    "type": "json_schema",
                    "name": "route_proposal",
                    "strict": True,
                    "schema": PROPOSAL_JSON_SCHEMA,
                }
            },
        )
        try:
            text = response.output[0].content[0].text
        except (AttributeError, IndexError, TypeError) as exc:
            raise RoutingAgentError("router response missing content") from exc
        if not isinstance(text, str) or not text.strip():
            raise RoutingAgentError("router response missing content")
        data = self._parse_router_response(text)
        data.setdefault("router_invoked", True)
        data.setdefault("router_fallback_used", fallback)
        data.setdefault("proposal_source", "llm_router")
        data.setdefault("proposal_source_label", "Router recommendation")
        data.setdefault("source_input_sha256_prefix", hash_user_message(user_message))
        data.setdefault("source_extracted_chars", len(user_message))
        proposal = _route_proposal_from_data(data)
        proposal = validate_route_proposal(proposal)
        return _enforce_permission_floor(proposal, user_message)

    @staticmethod
    def _parse_router_response(raw: str) -> dict[str, Any]:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RoutingAgentError("invalid JSON from router") from exc
        if not isinstance(data, dict):
            raise RoutingAgentError("router JSON must be an object")
        return data

    async def _invoke_router(
        self, model: str, prompt: str, *, fallback: bool = False
    ) -> tuple[str, dict[str, Any]]:
        headers = {"content-type": "application/json"}
        if self.api_key:
            headers["authorization"] = f"Bearer {self.api_key}"
        body = {
            "model": model,
            "messages": [{"role": "system", "content": prompt}],
            "temperature": 0,
            # Local OmniRoute returns SSE by default; force a single JSON
            # body so ``resp.json()`` works without consuming a stream.
            "stream": False,
            "response_format": {"type": "json_object"},
        }
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            resp = await client.post(self.api_url, headers=headers, json=body)
            resp.raise_for_status()
            payload = resp.json()
            provenance = extract_evaluator_provenance(
                requested_model=model,
                payload=payload,
                headers=resp.headers,
                fallback_model=self.fallback_model,
                outer_fallback_used=fallback,
            )
        content = payload.get("choices", [{}])[0].get("message", {}).get("content")
        if not isinstance(content, str) or not content.strip():
            raise RoutingAgentError("router response missing content")
        return content, provenance

    def _prompt(
        self, user_message: str, available_harnesses: list[str] | None, billing_policy: str
    ) -> str:
        routes = [asdict(p) for p in OMNIROUTE_ROUTE_CATALOG.values()]
        examples = "\n".join(
            f"Example {i + 1}: {ex}" for i, ex in enumerate(_ROUTER_PROMPT_EXAMPLES)
        )
        return "\n".join(
            [
                "You are Omnigent's Model Routing Agent. Return strict JSON only.",
                f"User message (JSON string): {json.dumps(user_message)}",
                f"Available harnesses: {available_harnesses or ['OpenCode Native']}",
                f"Allowed permission modes: {sorted(KNOWN_PERMISSION_MODES)}",
                f"Billing policy: {billing_policy}",
                f"Available native OmniRoute routes: {json.dumps(routes, separators=(',', ':'))}",
                _ROUTER_PROMPT_RULES,
                _ROUTER_PROMPT_FIELDS,
                examples,
            ]
        )


def build_routing_agent_from_runtime() -> RoutingAgent:
    """Build a :class:`RoutingAgent` using the active runtime configuration.

    Preferred path: pass a :class:`PolicyLLMClient` constructed from
    ``RuntimeCaps.llm`` so the routing agent reuses the server-level
    LLM (the same wiring :mod:`omnigent.server.smart_routing` uses).
    Falls back to env-var httpx when no server-level LLM is set.

    Connection resolution honours ``policy_llm_connection_factory``
    (managed deployments that bill per-caller) before falling back
    to the static ``llm.connection`` / ``llm.profile`` path used by
    the policy engine.

    Never raises — returns a RoutingAgent that may fail-closed when
    invoked; callers (``_await_route_approval``) translate that to
    the user-facing error. The returned agent always has a config;
    no caller should ever instantiate ``RoutingAgent()`` with empty
    kwargs again, since that silently routes to ``httpx`` env vars
    the server does not necessarily have.
    """
    try:
        from omnigent.runtime._globals import get_caps
        from omnigent.runtime.policies.builder import (
            _build_policy_llm_client,
            _resolve_server_llm_connection,
        )
    except ImportError:
        get_caps = None  # type: ignore[assignment]

    policy_client: Any | None = None
    if get_caps is not None:
        try:
            caps = get_caps()
        except Exception:  # noqa: BLE001  # never propagate
            caps = None
        server_llm = getattr(caps, "llm", None) if caps is not None else None
        if server_llm is not None:
            connection: dict[str, str] | None = None
            factory = (
                getattr(caps, "policy_llm_connection_factory", None) if caps is not None else None
            )
            if callable(factory):
                try:
                    connection = factory()
                except Exception:  # noqa: BLE001
                    connection = None
            if not connection:
                try:
                    connection = _resolve_server_llm_connection(server_llm)
                except Exception:  # noqa: BLE001
                    connection = None
            policy_client = _build_policy_llm_client(server_llm, connection)

    return RoutingAgent(policy_llm_client=policy_client)
