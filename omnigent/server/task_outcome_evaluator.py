"""LLM evaluator for completed task runs.

After a task reaches a terminal state (completed / failed /
cancelled / incomplete), this module runs a single structured
evaluation through the existing ``PolicyLLMClient`` — the same
client the routing agent and policy callables already use. That
client is built from ``RuntimeCaps.llm`` so every inference goes
``Omnigent → OmniRoute → selected evaluator model`` with no
direct OpenAI / Anthropic / OpenRouter / etc. provider calls.

The evaluator produces a :class:`TaskEvaluation` row only after the fixed
``minimax/MiniMax-M3`` model returned valid structured output and OmniRoute
proved that no fallback occurred. Availability failures remain durable on the
run as ``deferred``; operator/configuration/protocol failures become ``failed``.

Evaluation budget
-----------------
One M3 call normally; malformed structured output permits one immediate retry.
All later availability retries are durable, conservatively scheduled, and
bounded by the server worker policy.

What we never send
------------------
- Credentials, environment variables, hostnames
- Full file contents or diffs
- Tool-call argument text that may contain secrets (we only
  include the tool-call ``name`` and a bounded summary)
- Anything from ``changed_files`` beyond a count + commit SHA
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.entities.task_outcome import (
    EVALUATOR_ACCURACY_VALUES,
    TASK_FAMILIES,
    TASK_VERDICTS,
    TaskEvaluation,
    TaskRun,
)
from omnigent.llms.errors import PermanentLLMError, RetryableLLMError
from omnigent.runtime.policies.builder import (
    _build_policy_llm_client,
    _resolve_server_llm_connection,
)
from omnigent.stores.task_outcome_store import (
    CreateTaskEvaluationInput,
    TaskOutcomeStore,
)

_logger = logging.getLogger(__name__)


# Maximum characters of sanitised content we send in the prompt.
# The evaluator operates on bounded excerpts only — credentials /
# repo contents must NEVER reach the evaluator. Each bound is
# generous enough that the evaluator can reason about the task but
# tight enough that a large repo doesn't blow the model's context.
_MAX_TASK_DESCRIPTION_CHARS = 2_000
_MAX_RESPONSE_SUMMARY_CHARS = 2_000
_MAX_UNRESOLVED_ERROR_CHARS = 1_000
_MAX_EVIDENCE_ITEMS = 10
_MAX_UNRESOLVED_ITEMS = 10
_MAX_PROMPT_CHARS = 24_000


@dataclass(frozen=True)
class EvaluatorEvidence:
    """Sanitised objective evidence handed to the LLM evaluator.

    Mirrors what the spec lists as "available objective evidence":
    terminal status, harness exit info, available checks (tests /
    lint / typecheck / build), changed files, commit SHA,
    duration / tokens, retries, timeout / cancellation. Each
    field is optional — none of these are required, and the
    evaluator must NOT invent success evidence when a field is
    absent (the prompt explicitly forbids it).

    :param terminal_status: ``completed`` / ``failed`` /
        ``cancelled`` / ``incomplete``.
    :param harness_exit_code: Harness / command exit status when
        reported. ``None`` when not surfaced.
    :param tests_passed: ``True`` when the harness explicitly
        reported a passing test run. ``None`` when not reported —
        distinct from ``False`` so the evaluator can tell "ran
        tests and they failed" from "didn't run tests".
    :param tests_failed: Counterpart for explicit failures.
    :param lint_passed / typecheck_passed / build_passed: Same
        tri-state pattern (``True``/``False``/``None``).
    :param changed_files_count: Number of changed files the
        harness reported (the names themselves are NOT sent to
        the evaluator to avoid leaking repo contents).
    :param commit_sha: Git commit SHA when the harness surfaced
        it.
    :param retries: Number of retries observed.
    :param timed_out: ``True`` when the harness surfaced a
        timeout.
    :param cancelled: ``True`` when the user cancelled the task.
    :param duration_ms: ``terminal_at - started_at`` in ms.
    :param input_tokens / output_tokens: Token counts when priced
        (best-effort).
    :param total_cost_usd: Catalog-priced or harness-reported USD.
    """

    terminal_status: str
    harness_exit_code: int | None = None
    tests_passed: bool | None = None
    tests_failed: bool | None = None
    lint_passed: bool | None = None
    typecheck_passed: bool | None = None
    build_passed: bool | None = None
    changed_files_count: int | None = None
    commit_sha: str | None = None
    retries: int | None = None
    timed_out: bool | None = None
    cancelled: bool | None = None
    duration_ms: int | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_cost_usd: float | None = None


def collect_evidence(task_run: TaskRun) -> EvaluatorEvidence:
    """Build an :class:`EvaluatorEvidence` from a terminalised :class:`TaskRun`.

    The evidence is the only "objective" data the LLM evaluator
    sees — every field is either reported by the harness, derived
    from the run row, or ``None``. No parsing of the agent's
    prose to "guess" test results: a missing field is reported
    as unavailable, never passed.

    :param task_run: The terminalised :class:`TaskRun`.
    :returns: The :class:`EvaluatorEvidence` snapshot.
    """
    changed_files_count = len(task_run.changed_files) if task_run.changed_files else None
    return EvaluatorEvidence(
        terminal_status=task_run.terminal_status,
        commit_sha=task_run.commit_sha,
        changed_files_count=changed_files_count,
        retries=None,  # Not yet tracked separately; reserved for a future field.
        timed_out=task_run.terminal_status == "incomplete"
        and task_run.failure_error_code in {"timeout", "deadline_exceeded"},
        cancelled=task_run.terminal_status == "cancelled",
        duration_ms=task_run.duration_ms,
        input_tokens=task_run.input_tokens,
        output_tokens=task_run.output_tokens,
        total_cost_usd=task_run.total_cost_usd,
    )


# ── Evaluation schema (passed to PolicyLLMClient.create's response_format) ─


EVALUATOR_JSON_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdict": {
            "type": "string",
            "enum": list(TASK_VERDICTS),
        },
        "confidence": {
            "type": "number",
            "minimum": 0.0,
            "maximum": 1.0,
        },
        "quality": {
            # 1-5 quality score; nullable so the evaluator can omit it.
            "type": ["integer", "null"],
            "minimum": 1,
            "maximum": 5,
        },
        "task_family": {
            "type": "string",
            "enum": list(TASK_FAMILIES),
        },
        "reasoning": {"type": "string", "minLength": 1},
        "evidence": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_EVIDENCE_ITEMS,
        },
        "unresolved_issues": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": _MAX_UNRESOLVED_ITEMS,
        },
    },
    "required": [
        "verdict",
        "confidence",
        "task_family",
        "reasoning",
        "evidence",
        "unresolved_issues",
    ],
    "additionalProperties": False,
}


# ── Prompt construction ────────────────────────────────────────────────────


def _truncate(text: str | None, limit: int) -> str | None:
    """Truncate *text* to *limit* characters, suffix ``…`` when clipped.

    Returns ``None`` for ``None`` / empty input so callers can
    distinguish "no value" from "empty value".
    """
    if text is None or not text:
        return None
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _format_evidence(evidence: EvaluatorEvidence) -> str:
    """Render :class:`EvaluatorEvidence` as a JSON object for the prompt.

    ``None`` fields are OMITTED so the LLM sees only the evidence
    that was actually observed. A ``True``/``False`` field is
    included so the evaluator can distinguish "ran tests and they
    passed" from "ran tests and they failed".
    """
    raw = {
        k: v
        for k, v in {
            "terminal_status": evidence.terminal_status,
            "harness_exit_code": evidence.harness_exit_code,
            "tests_passed": evidence.tests_passed,
            "tests_failed": evidence.tests_failed,
            "lint_passed": evidence.lint_passed,
            "typecheck_passed": evidence.typecheck_passed,
            "build_passed": evidence.build_passed,
            "changed_files_count": evidence.changed_files_count,
            "commit_sha": evidence.commit_sha,
            "retries": evidence.retries,
            "timed_out": evidence.timed_out,
            "cancelled": evidence.cancelled,
            "duration_ms": evidence.duration_ms,
            "input_tokens": evidence.input_tokens,
            "output_tokens": evidence.output_tokens,
            "total_cost_usd": evidence.total_cost_usd,
        }.items()
        if v is not None
    }
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=True)


def _format_routing(task_run: TaskRun) -> str:
    """Render the routing snapshot for the prompt.

    Only provenance fields are sent — no request bodies / responses
    / user message content. The evaluator uses this to assess
    "did the routing decision match the task difficulty?" but
    cannot use it to attribute or re-derive scores back to the
    router itself.
    """
    raw = {
        k: v
        for k, v in {
            "requested_route_id": task_run.requested_route_id,
            "selected_provider": task_run.selected_provider,
            "selected_model": task_run.selected_model,
            "reasoning_effort": task_run.reasoning_effort,
            "permission_mode": task_run.permission_mode,
            "omniroute_decision_id": task_run.omniroute_decision_id,
            "selection_strategy": task_run.selection_strategy,
            "billing_class": task_run.billing_class,
            "fallback_used": task_run.fallback_used,
        }.items()
        if v is not None
    }
    return json.dumps(raw, separators=(",", ":"), ensure_ascii=True)


def build_evaluator_prompt(
    task_run: TaskRun,
    *,
    triggering_message_summary: str | None,
    evidence: EvaluatorEvidence,
) -> str:
    """Build the evaluator's user prompt.

    Returns a bounded, sanitised string. NEVER includes raw repo
    contents, credentials, environment variables, or full diffs.

    :param task_run: The terminalised :class:`TaskRun`.
    :param triggering_message_summary: Sanitised summary of the
        user message that started the task. ``None`` when not
        available.
    :param evidence: The :class:`EvaluatorEvidence` snapshot.
    :returns: The full prompt string (≤ :data:`_MAX_PROMPT_CHARS`).
    """
    sections: list[str] = []
    sections.append(
        "You are an automated evaluator for an Omnigent coding task. "
        "Read the task summary, terminal status, available objective "
        "evidence, and routing provenance, then return strict JSON "
        "matching the provided schema. Do not invent evidence. Do "
        "not include raw repo contents, diffs, or credentials. "
        "If a field is absent from the evidence object, treat it "
        "as unavailable, NOT as passed."
    )
    sections.append("Verdict vocabulary: " + ", ".join(TASK_VERDICTS) + ".")
    sections.append("Task family vocabulary: " + ", ".join(TASK_FAMILIES) + ".")
    sections.append("\n--- TASK ---")
    if triggering_message_summary:
        sections.append(
            "Original user request (sanitised summary):\n"
            + _truncate(triggering_message_summary, _MAX_TASK_DESCRIPTION_CHARS)
        )
    if task_run.task_description:
        sections.append(
            "Sanitised task description:\n"
            + _truncate(task_run.task_description, _MAX_TASK_DESCRIPTION_CHARS)
        )
    sections.append("\n--- ROUTING PROVENANCE ---\n" + _format_routing(task_run))
    sections.append(
        "\n--- AVAILABLE OBJECTIVE EVIDENCE ---"
        "\n"
        + _format_evidence(evidence)
        + "\nNotes:\n"
        + "- 'tests_passed' / 'tests_failed' / 'lint_passed' / "
        + "'typecheck_passed' / 'build_passed' are NULL when the "
        + 'harness did NOT report them. NULL means "unavailable", '
        + 'NOT "passed".\n'
        + "- 'changed_files_count' is the number of files the harness "
        + "reported; the file paths themselves are NOT shared with "
        + "you for privacy reasons.\n"
        + "- 'duration_ms' is wall-clock from execution start to "
        + "terminal event.\n"
        + "- 'total_cost_usd' may be NULL when pricing is unavailable; "
        + "do not infer cost from model name.\n"
    )
    if task_run.response_summary:
        sections.append(
            "\n--- FINAL AGENT RESPONSE (truncated) ---"
            "\n" + _truncate(task_run.response_summary, _MAX_RESPONSE_SUMMARY_CHARS)
        )
    if task_run.failure_error_code or task_run.failure_error_message:
        err_code = task_run.failure_error_code or ""
        err_message = _truncate(task_run.failure_error_message, _MAX_UNRESOLVED_ERROR_CHARS) or ""
        sections.append(
            f"\n--- REPORTED UNRESOLVED ERROR ---\ncode: {err_code}\nmessage: {err_message}"
        )
    sections.append(
        "\n--- OUTPUT SCHEMA ---"
        "\nReturn strict JSON only, no prose, no markdown fences, no "
        "extra keys."
    )

    prompt = "\n".join(sections)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[: _MAX_PROMPT_CHARS - 1] + "…"
    return prompt


# ── Fixed M3 inference ───────────────────────────────────────────────────


def _system_prompt() -> str:
    return (
        "You are Omnigent's Task Outcome Evaluator. Given a sanitised task summary, "
        "terminal execution status, available objective evidence, and routing provenance, "
        "return strict JSON matching the provided schema only. Never add markdown, prose, "
        "or extra keys. Verdict vocabulary: "
        + ", ".join(TASK_VERDICTS)
        + ". Task family vocabulary: "
        + ", ".join(TASK_FAMILIES)
        + ". Treat absent evidence as unavailable, never as passed. Use 'inconclusive' "
        "when the evidence does not let you decide."
    )


def _validate_evaluator_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("evaluator payload must be a JSON object")
    verdict = payload.get("verdict")
    if verdict not in TASK_VERDICTS:
        raise ValueError(f"evaluator verdict must be one of {TASK_VERDICTS!r}")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool):
        raise ValueError("evaluator confidence must be a number 0..1")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise ValueError("evaluator confidence is out of range")
    quality = payload.get("quality")
    if quality is not None and (
        not isinstance(quality, int) or isinstance(quality, bool) or not 1 <= quality <= 5
    ):
        raise ValueError("evaluator quality must be an integer 1..5")
    task_family = payload.get("task_family")
    if task_family not in TASK_FAMILIES:
        raise ValueError(f"evaluator task_family must be one of {TASK_FAMILIES!r}")
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("evaluator reasoning must be a non-empty string")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(item, str) for item in evidence):
        raise ValueError("evaluator evidence must be a list of strings")
    unresolved = payload.get("unresolved_issues")
    if not isinstance(unresolved, list) or not all(isinstance(item, str) for item in unresolved):
        raise ValueError("evaluator unresolved_issues must be a list of strings")
    return {
        "verdict": verdict,
        "confidence": confidence,
        "quality": quality,
        "task_family": task_family,
        "reasoning": reasoning.strip(),
        "evidence": list(evidence)[:_MAX_EVIDENCE_ITEMS],
        "unresolved_issues": list(unresolved)[:_MAX_UNRESOLVED_ITEMS],
    }


FIXED_EVALUATOR_MODEL = "minimax/MiniMax-M3"
_OMNIROUTE_TRANSPORT_MODEL = f"omniroute/{FIXED_EVALUATOR_MODEL}"
_EXPECTED_PROVIDER = "minimax"
_DEFAULT_AUTO_RETRY_DELAYS = (300, 1_800, 7_200, 21_600, 43_200)
_TRANSIENT_HTTP_STATUSES = frozenset({408, 429, 502, 503, 504})
_TRANSIENT_MARKERS = (
    "all_accounts_inactive",
    "all accounts inactive",
    "cooldown",
    "quota",
    "rate limit",
    "rate_limit",
    "plan exhausted",
    "credits exhausted",
    "temporarily unavailable",
    "provider unavailable",
)
_SECRET_PATTERN = re.compile(
    r"(?i)(authorization|bearer|api[_-]?key|token|secret|password)(\s*[:=]?\s*)([^\s,;]+)"
)


@dataclass(frozen=True)
class EvaluatorFailure:
    """Sanitized classification for a failed M3 attempt."""

    kind: str
    code: str
    message: str
    transient: bool


@dataclass(frozen=True)
class EvaluatorProvenance:
    """Verified OmniRoute response provenance."""

    provider: str
    model: str
    fallback_used: bool
    decision_id: str | None = None


@dataclass(frozen=True)
class EvaluatorOutcome:
    """Durable result of one requested evaluator attempt."""

    status: str
    evaluation: TaskEvaluation | None = None
    failure: EvaluatorFailure | None = None
    langfuse_evaluation_id: str | None = None


def _sanitize_error(value: object, limit: int = 1000) -> str:
    text = " ".join(str(value).split())
    text = _SECRET_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}<redacted>", text)
    return (text or "Evaluator request failed")[:limit]


def _error_detail(exc: BaseException) -> tuple[int | None, str | None, str]:
    """Extract HTTP status, structured code, and bounded text from a chain."""
    seen: set[int] = set()
    current: BaseException | None = exc
    status: int | None = None
    code: str | None = None
    parts: list[str] = []
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        detail = getattr(current, "detail", None)
        detail_status = getattr(detail, "status_code", None)
        if isinstance(detail_status, int):
            status = detail_status
        detail_body = getattr(detail, "response_body", None)
        if detail_body:
            parts.append(str(detail_body))
        current_code = getattr(current, "code", None)
        if current_code is not None:
            code = str(current_code)
        if isinstance(current, httpx.HTTPStatusError):
            status = current.response.status_code
            with suppress(Exception):
                parts.append(current.response.text)
        parts.append(str(current))
        chained = current.__cause__ or current.__context__
        current = chained if isinstance(chained, BaseException) else None
    rendered = _sanitize_error(" | ".join(part for part in parts if part))
    try:
        parsed = json.loads(rendered.split(" | ", 1)[0])
        error = parsed.get("error", parsed) if isinstance(parsed, dict) else None
        if isinstance(error, dict):
            structured_code = error.get("code") or error.get("type")
            if structured_code:
                code = str(structured_code)
            structured_message = error.get("message")
            if structured_message:
                rendered = _sanitize_error(structured_message)
    except (json.JSONDecodeError, AttributeError):
        pass
    return status, code, rendered


def classify_evaluator_error(exc: BaseException) -> EvaluatorFailure:
    """Classify one failure without turning it into an evaluator verdict."""
    status, structured_code, message = _error_detail(exc)
    lowered = f"{structured_code or ''} {message}".lower()
    if isinstance(exc, (TimeoutError, httpx.TimeoutException)):
        return EvaluatorFailure("availability", "timeout", message, True)
    if isinstance(exc, (ConnectionError, httpx.NetworkError)):
        return EvaluatorFailure("availability", "connection_error", message, True)
    if isinstance(exc, RetryableLLMError):
        code = structured_code or getattr(exc, "code", None) or "temporarily_unavailable"
        return EvaluatorFailure("availability", str(code), message, True)
    if status in _TRANSIENT_HTTP_STATUSES or any(
        marker in lowered for marker in _TRANSIENT_MARKERS
    ):
        return EvaluatorFailure(
            "availability", structured_code or str(status or "unavailable"), message, True
        )
    if status in {401, 403}:
        return EvaluatorFailure("authentication", structured_code or str(status), message, False)
    if status in {400, 404}:
        return EvaluatorFailure("configuration", structured_code or str(status), message, False)
    if isinstance(exc, PermanentLLMError):
        return EvaluatorFailure(
            "configuration", structured_code or "llm_request_rejected", message, False
        )
    return EvaluatorFailure("internal", structured_code or "internal_error", message, False)


def _configured_policy_client() -> tuple[Any | None, EvaluatorFailure | None]:
    try:
        from omnigent.runtime import get_caps

        caps = get_caps()
    except Exception as exc:  # noqa: BLE001
        return None, EvaluatorFailure(
            "configuration", "runtime_caps_unavailable", _sanitize_error(exc), False
        )
    server_llm = getattr(caps, "llm", None)
    if server_llm is None:
        return None, EvaluatorFailure(
            "configuration",
            "missing_evaluator_config",
            "Server-level evaluator configuration is missing (RuntimeCaps.llm is None).",
            False,
        )
    configured_model = getattr(server_llm, "model", None)
    # The bare concrete model id is routed through the configured gateway.
    allowed_models = {FIXED_EVALUATOR_MODEL, _OMNIROUTE_TRANSPORT_MODEL}
    if configured_model not in allowed_models:
        return None, EvaluatorFailure(
            "configuration",
            "invalid_evaluator_model",
            f"Evaluator must target {FIXED_EVALUATOR_MODEL} "
            f"(got {_sanitize_error(configured_model)!r}).",
            False,
        )
    try:
        connection = None
        factory = getattr(caps, "policy_llm_connection_factory", None)
        if callable(factory):
            connection = factory()
        if not connection:
            connection = _resolve_server_llm_connection(server_llm)
        if not isinstance(connection, dict) or not connection.get("base_url"):
            return None, EvaluatorFailure(
                "configuration",
                "invalid_omniroute_endpoint",
                "Evaluator OmniRoute connection is missing a base_url.",
                False,
            )
        if not connection.get("api_key"):
            return None, EvaluatorFailure(
                "authentication",
                "missing_omniroute_credential",
                "Evaluator OmniRoute credential is missing.",
                False,
            )
        return _build_policy_llm_client(server_llm, connection), None
    except Exception as exc:  # noqa: BLE001
        failure = classify_evaluator_error(exc)
        return None, EvaluatorFailure("configuration", failure.code, failure.message, False)


def _verify_provenance(
    response: Any,
) -> tuple[EvaluatorProvenance | None, EvaluatorFailure | None]:
    metadata = getattr(response, "provider_metadata", None)
    if not isinstance(metadata, dict):
        return None, EvaluatorFailure(
            "provenance",
            "missing_provenance",
            "OmniRoute response provenance headers are missing.",
            False,
        )
    requested = metadata.get("x-omniroute-requested-model")
    provider = metadata.get("x-omniroute-selected-provider")
    model = metadata.get("x-omniroute-selected-model")
    fallback = metadata.get("x-omniroute-fallback-used")
    if requested != FIXED_EVALUATOR_MODEL:
        return None, EvaluatorFailure(
            "provenance",
            "requested_model_mismatch",
            f"OmniRoute reported requested model {requested!r}, not {FIXED_EVALUATOR_MODEL}.",
            False,
        )
    if provider != _EXPECTED_PROVIDER or model != FIXED_EVALUATOR_MODEL:
        return None, EvaluatorFailure(
            "provenance",
            "unexpected_evaluator_model",
            "No-fallback invariant violated: OmniRoute selected "
            f"{provider or 'unknown'}/{model or 'unknown'}.",
            False,
        )
    if fallback != "false":
        return None, EvaluatorFailure(
            "provenance",
            "fallback_detected",
            "No-fallback invariant violated: OmniRoute reported fallback use.",
            False,
        )
    return EvaluatorProvenance(
        provider=provider,
        model=model,
        fallback_used=False,
        decision_id=metadata.get("x-omniroute-decision-id"),
    ), None


def _extract_response_text(response: Any) -> str:
    try:
        text = response.output[0].content[0].text
    except (AttributeError, IndexError, TypeError) as exc:
        raise ValueError("M3 response is missing text content") from exc
    if not isinstance(text, str) or not text.strip():
        raise ValueError("M3 response is missing text content")
    return text


def _protocol_payload(response: Any) -> dict[str, Any]:
    text = _extract_response_text(response)
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"M3 response is not valid JSON: {exc.msg}") from exc
    return _validate_evaluator_payload(payload)


def evaluator_retry_delays() -> tuple[int, ...]:
    raw = os.environ.get("OMNIGENT_EVALUATOR_RETRY_DELAYS_SECONDS")
    if not raw:
        return _DEFAULT_AUTO_RETRY_DELAYS
    try:
        values = tuple(int(value.strip()) for value in raw.split(",") if value.strip())
    except ValueError:
        _logger.error("Invalid OMNIGENT_EVALUATOR_RETRY_DELAYS_SECONDS; using defaults")
        return _DEFAULT_AUTO_RETRY_DELAYS
    return (
        values if values and all(value >= 60 for value in values) else _DEFAULT_AUTO_RETRY_DELAYS
    )


def next_retry_at(attempt_count: int, *, now: int | None = None) -> int | None:
    delays = evaluator_retry_delays()
    index = attempt_count - 1
    if index < 0 or index >= len(delays):
        return None
    return (now if now is not None else int(time.time())) + delays[index]


async def evaluate_task_outcome(
    store: TaskOutcomeStore,
    task_run: TaskRun,
    *,
    triggering_message_summary: str | None = None,
) -> EvaluatorOutcome:
    """Attempt one provenance-verified fixed M3 judgment and persist its lifecycle."""
    from omnigent.server.langfuse_sync import langfuse_idempotency_key

    if task_run.evaluation_status != "pending":
        failure = EvaluatorFailure(
            "scheduling",
            "attempt_not_pending",
            f"Run {task_run.id} was dispatched without a pending evaluator claim.",
            False,
        )
        return EvaluatorOutcome("failed", failure=failure)

    client, config_failure = _configured_policy_client()
    if config_failure is not None or client is None:
        failure = config_failure or EvaluatorFailure(
            "configuration", "missing_client", "Evaluator client is unavailable.", False
        )
        store.mark_evaluation_failed(
            task_run.id,
            error_kind=failure.kind,
            error_code=failure.code,
            error_message=failure.message,
        )
        return EvaluatorOutcome("failed", failure=failure)

    prompt = build_evaluator_prompt(
        task_run,
        triggering_message_summary=triggering_message_summary,
        evidence=collect_evidence(task_run),
    )
    protocol_failure: EvaluatorFailure | None = None
    for protocol_try in range(2):
        try:
            response = await client.create(
                input=[{"role": "user", "content": [{"type": "input_text", "text": prompt}]}],
                instructions=_system_prompt(),
                text={
                    "format": {
                        "type": "json_schema",
                        "name": "task_outcome_evaluation",
                        "strict": True,
                        "schema": EVALUATOR_JSON_SCHEMA,
                    }
                },
            )
        except Exception as exc:  # noqa: BLE001
            failure = classify_evaluator_error(exc)
            if failure.transient:
                retry_at = next_retry_at(task_run.evaluation_attempt_count)
                message = failure.message
                if retry_at is None:
                    message += " Automatic retry budget exhausted; manual retry remains available."
                store.mark_evaluation_deferred(
                    task_run.id,
                    error_kind=failure.kind,
                    error_code=failure.code,
                    error_message=message,
                    next_retry_at=retry_at,
                )
                return EvaluatorOutcome("deferred", failure=failure)
            store.mark_evaluation_failed(
                task_run.id,
                error_kind=failure.kind,
                error_code=failure.code,
                error_message=failure.message,
            )
            return EvaluatorOutcome("failed", failure=failure)

        provenance, provenance_failure = _verify_provenance(response)
        if provenance_failure is not None or provenance is None:
            failure = provenance_failure or EvaluatorFailure(
                "provenance", "invalid_provenance", "M3 provenance validation failed.", False
            )
            store.mark_evaluation_failed(
                task_run.id,
                error_kind=failure.kind,
                error_code=failure.code,
                error_message=failure.message,
            )
            return EvaluatorOutcome("failed", failure=failure)
        try:
            normalized = _protocol_payload(response)
            break
        except ValueError as exc:
            protocol_failure = EvaluatorFailure(
                "protocol", "malformed_structured_output", _sanitize_error(exc), False
            )
            if protocol_try == 0:
                _logger.warning(
                    "M3 returned malformed output for run=%s; retrying once", task_run.id
                )
                continue
            store.mark_evaluation_failed(
                task_run.id,
                error_kind=protocol_failure.kind,
                error_code=protocol_failure.code,
                error_message=protocol_failure.message,
            )
            return EvaluatorOutcome("failed", failure=protocol_failure)
    else:  # pragma: no cover - loop always returns or breaks
        assert protocol_failure is not None
        return EvaluatorOutcome("failed", failure=protocol_failure)

    evaluation = store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=task_run.id,
            evaluator_type="llm",
            evaluator_provider=provenance.provider,
            evaluator_model=provenance.model,
            evaluator_route_id=FIXED_EVALUATOR_MODEL,
            evaluator_fallback_used=provenance.fallback_used,
            evaluator_decision_id=provenance.decision_id,
            verdict=normalized["verdict"],
            confidence=normalized["confidence"],
            quality_score=normalized["quality"],
            proposed_task_family=normalized["task_family"],
            reasoning=normalized["reasoning"],
            evidence=normalized["evidence"],
            unresolved_issues=normalized["unresolved_issues"],
        )
    )
    return EvaluatorOutcome(
        "completed",
        evaluation=evaluation,
        langfuse_evaluation_id=langfuse_idempotency_key(task_run.id, "llm-verdict"),
    )


__all__ = [
    "EVALUATOR_ACCURACY_VALUES",
    "EVALUATOR_JSON_SCHEMA",
    "FIXED_EVALUATOR_MODEL",
    "EvaluatorEvidence",
    "EvaluatorFailure",
    "EvaluatorOutcome",
    "build_evaluator_prompt",
    "classify_evaluator_error",
    "collect_evidence",
    "evaluate_task_outcome",
    "evaluator_retry_delays",
    "next_retry_at",
]
