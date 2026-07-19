"""LLM evaluator for completed task runs.

After a task reaches a terminal state (completed / failed /
cancelled / incomplete), this module runs a single structured
evaluation through the existing ``PolicyLLMClient`` — the same
client the routing agent and policy callables already use. That
client is built from ``RuntimeCaps.llm`` so every inference goes
``Omnigent → OmniRoute → selected evaluator model`` with no
direct OpenAI / Anthropic / OpenRouter / etc. provider calls.

The evaluator produces a single :class:`TaskEvaluation` row. On
failure (HTTP error, invalid JSON, schema violation) it records a
``verdict='inconclusive'`` row so the schema contract is "always
exactly one evaluation per task run" — the review-card UI never
needs to JOIN to know whether the evaluator ran.

Evaluation budget
-----------------
A single ``PolicyLLMClient.create()`` call. Bounded prompt (~6 KB
of sanitised task summary + provenance) so a hard-token-limit
model can still answer. Strict JSON schema enforced by the
underlying Responses API.

We deliberately don't retry on transient failures — a stuck
review card is worse than a missing evaluation. The relay
schedules the next evaluation by simply waiting for the next
``response.completed`` (we never re-evaluate an existing task
run; if the evaluator fails, the row is ``inconclusive`` and
the operator can re-evaluate manually via a future endpoint).

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
from dataclasses import dataclass
from typing import Any

from omnigent.entities.task_outcome import (
    EVALUATOR_ACCURACY_VALUES,
    TASK_FAMILIES,
    TASK_VERDICTS,
    TaskEvaluation,
    TaskRun,
)
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


# ── Inference ─────────────────────────────────────────────────────────────


def _policy_llm_client_or_none() -> Any:
    """Build the server-level :class:`PolicyLLMClient`, or ``None``.

    Reuses the same wiring :func:`build_routing_agent_from_runtime`
    uses, so the LLM evaluator's inference path is bit-for-bit the
    same OmniRoute-routed path the routing agent uses. The
    underlying ``Client`` is created lazily; this function is
    cheap to call and safe to invoke at any time.

    :returns: A :class:`PolicyLLMClient` when ``RuntimeCaps.llm``
        is configured; ``None`` otherwise.
    """
    try:
        from omnigent.runtime._globals import get_caps
    except ImportError:
        return None

    try:
        caps = get_caps()
    except Exception:  # noqa: BLE001  # never propagate
        caps = None
    server_llm = getattr(caps, "llm", None) if caps is not None else None
    if server_llm is None:
        return None
    connection: dict[str, str] | None = None
    factory = getattr(caps, "policy_llm_connection_factory", None) if caps is not None else None
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
    return _build_policy_llm_client(server_llm, connection)


def _system_prompt() -> str:
    """Return the evaluator's system prompt.

    Returns:
        System prompt string emphasising: strict JSON, no invented
        evidence, bounded evidence interpretation, and the verdict
        vocabulary.
    """
    return (
        "You are Omnigent's Task Outcome Evaluator. Given a sanitised "
        "task summary, terminal execution status, available objective "
        "evidence, and routing provenance, return strict JSON matching "
        "the provided schema only. Never add markdown, prose, or extra "
        "keys. Verdict vocabulary: "
        + ", ".join(TASK_VERDICTS)
        + ". Task family vocabulary: "
        + ", ".join(TASK_FAMILIES)
        + ". If a piece of evidence is unavailable (absent from the "
        "evidence object), treat it as unavailable — never as passed. "
        "Use 'inconclusive' when the evidence does not let you decide."
    )


def _validate_evaluator_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Strict-validate an evaluator payload against :data:`EVALUATOR_JSON_SCHEMA`.

    :param payload: Decoded JSON dict from the evaluator.
    :returns: A normalised payload (coerced types, defaulted
        ``quality``) safe to write to :class:`TaskEvaluation`.
    :raises ValueError: When *payload* violates the schema. The
        caller converts this to an ``inconclusive`` row.
    """
    if not isinstance(payload, dict):
        raise ValueError("evaluator payload must be a JSON object")
    verdict = payload.get("verdict")
    if verdict not in TASK_VERDICTS:
        raise ValueError(f"evaluator verdict must be one of {TASK_VERDICTS!r}, got {verdict!r}")
    confidence = payload.get("confidence")
    if not isinstance(confidence, (int, float)):
        raise ValueError("evaluator confidence must be a number 0..1")
    confidence = float(confidence)
    if not (0.0 <= confidence <= 1.0):
        raise ValueError(f"evaluator confidence {confidence} out of range [0, 1]")
    quality = payload.get("quality")
    if quality is not None:
        if not isinstance(quality, int) or isinstance(quality, bool):
            raise ValueError("evaluator quality must be an integer 1..5")
        if not (1 <= quality <= 5):
            raise ValueError(f"evaluator quality {quality} out of range [1, 5]")
    task_family = payload.get("task_family")
    if task_family not in TASK_FAMILIES:
        raise ValueError(
            f"evaluator task_family must be one of {TASK_FAMILIES!r}, got {task_family!r}"
        )
    reasoning = payload.get("reasoning")
    if not isinstance(reasoning, str) or not reasoning.strip():
        raise ValueError("evaluator reasoning must be a non-empty string")
    evidence = payload.get("evidence")
    if not isinstance(evidence, list) or not all(isinstance(x, str) for x in evidence):
        raise ValueError("evaluator evidence must be a list of strings")
    evidence = list(evidence)[:_MAX_EVIDENCE_ITEMS]
    unresolved_issues = payload.get("unresolved_issues")
    if not isinstance(unresolved_issues, list) or not all(
        isinstance(x, str) for x in unresolved_issues
    ):
        raise ValueError("evaluator unresolved_issues must be a list of strings")
    unresolved_issues = list(unresolved_issues)[:_MAX_UNRESOLVED_ITEMS]

    return {
        "verdict": verdict,
        "confidence": confidence,
        "quality": quality,
        "task_family": task_family,
        "reasoning": reasoning.strip(),
        "evidence": evidence,
        "unresolved_issues": unresolved_issues,
    }


def _extract_provenance(policy_client: Any) -> dict[str, Any]:
    """Pull evaluator provenance from the configured :class:`PolicyLLMClient`.

    The :class:`PolicyLLMClient` doesn't expose its model +
    connection directly (those are private to the
    ``Client.responses.create`` call), so we read what the
    routing-agent path already records: the configured model on
    ``RuntimeCaps.llm``, the resolved connection, and the policy
    LLM client's request timeout. The trace of the actual
    OmniRoute-routed call is captured per-request via the same
    response headers the routing agent uses — a future iteration
    can plumb that through. For now the configured model + route
    are the best provenance we can record at this layer.

    :param policy_client: The configured
        :class:`PolicyLLMClient` (or ``None``).
    :returns: A dict of ``evaluator_provider`` / ``evaluator_model``
        / ``evaluator_route_id`` for the ``task_evaluations`` row.
        Empty when ``policy_client`` is ``None`` (the call will
        fail anyway).
    """
    if policy_client is None:
        return {}
    model = getattr(policy_client, "_model", None) or None
    provider = None
    if isinstance(model, str) and "/" in model:
        provider = model.split("/", 1)[0]
    route_id = None
    # ``RuntimeCaps.llm`` is the configured ``LLMConfig``; when the
    # spec uses ``route_id`` we record that as the evaluator's
    # ``route_id`` so the row tracks the routing path.
    try:
        from omnigent.runtime._globals import get_caps

        caps = get_caps()
    except ImportError:
        caps = None
    server_llm = getattr(caps, "llm", None) if caps is not None else None
    if server_llm is not None:
        route_id = getattr(server_llm, "route_id", None)
    return {
        "evaluator_provider": provider,
        "evaluator_model": model,
        "evaluator_route_id": route_id,
    }


# ── Public entrypoint ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class EvaluatorOutcome:
    """Result of one :func:`evaluate_task_outcome` call.

    :param evaluation: The persisted :class:`TaskEvaluation` row
        (always present — a failure is recorded as
        ``verdict='inconclusive'``).
    :param langfuse_evaluation_id: Stable ``id`` field for the
        Langfuse score (== ``langfuse_idempotency_key(run, "llm-verdict")``).
        Pre-computed so the relay can enqueue without a second
        hash.
    """

    evaluation: TaskEvaluation
    langfuse_evaluation_id: str


async def evaluate_task_outcome(
    store: TaskOutcomeStore,
    task_run: TaskRun,
    *,
    triggering_message_summary: str | None = None,
) -> EvaluatorOutcome:
    """Run the LLM evaluator and persist the :class:`TaskEvaluation` row.

    Always writes exactly one row (success or
    ``verdict='inconclusive'``); never raises. The review-card
    UI depends on the schema invariant "always exactly one
    evaluation per task run".

    Inference goes through :class:`PolicyLLMClient` (i.e.
    ``Omnigent → OmniRoute → selected evaluator model``). When
    ``RuntimeCaps.llm`` is unset, the call is short-circuited
    with an ``inconclusive`` row whose ``reasoning`` explains the
    miss — no direct provider fallback.

    :param store: The task-outcome store.
    :param task_run: The terminalised :class:`TaskRun` row.
    :param triggering_message_summary: Sanitised user-message
        summary to feed the evaluator (``None`` when not
        available; the prompt degrades gracefully).
    :returns: The :class:`EvaluatorOutcome` (always present).
    """
    from omnigent.server.langfuse_sync import langfuse_idempotency_key

    langfuse_evaluation_id = langfuse_idempotency_key(task_run.id, "llm-verdict")

    evidence = collect_evidence(task_run)
    prompt = build_evaluator_prompt(
        task_run,
        triggering_message_summary=triggering_message_summary,
        evidence=evidence,
    )
    policy_client = _policy_llm_client_or_none()
    provenance = _extract_provenance(policy_client)

    if policy_client is None:
        _logger.info(
            "task_outcome_evaluator: no server-level LLM configured for "
            "run=%s; recording inconclusive",
            task_run.id,
        )
        evaluation = store.create_evaluation(
            CreateTaskEvaluationInput(
                task_run_id=task_run.id,
                evaluator_type="llm",
                verdict="inconclusive",
                reasoning=(
                    "Automated evaluation unavailable: server-level LLM "
                    "configuration is not set (RuntimeCaps.llm is None)."
                ),
                evidence=None,
                unresolved_issues=None,
                **provenance,
            )
        )
        return EvaluatorOutcome(
            evaluation=evaluation,
            langfuse_evaluation_id=langfuse_evaluation_id,
        )

    try:
        response = await policy_client.create(
            input=[
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": prompt}],
                }
            ],
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
    except Exception as exc:  # noqa: BLE001  # never propagate
        _logger.warning(
            "task_outcome_evaluator: policy_client.create failed for run=%s: %s",
            task_run.id,
            exc,
        )
        evaluation = store.create_evaluation(
            CreateTaskEvaluationInput(
                task_run_id=task_run.id,
                evaluator_type="llm",
                verdict="inconclusive",
                reasoning=(
                    f"Automated evaluation unavailable: LLM call failed "
                    f"({type(exc).__name__}: {str(exc)[:400]!r}). "
                    "Human review still required."
                ),
                **provenance,
            )
        )
        return EvaluatorOutcome(
            evaluation=evaluation,
            langfuse_evaluation_id=langfuse_evaluation_id,
        )

    # Parse + validate the response. ``response.output[0].content[0].text``
    # is the Responses-API shape; tolerate other shapes defensively.
    text = None
    try:
        text = response.output[0].content[0].text
    except (AttributeError, IndexError, TypeError):
        text = None
    if not isinstance(text, str) or not text.strip():
        _logger.warning(
            "task_outcome_evaluator: missing text for run=%s response=%r",
            task_run.id,
            response,
        )
        evaluation = store.create_evaluation(
            CreateTaskEvaluationInput(
                task_run_id=task_run.id,
                evaluator_type="llm",
                verdict="inconclusive",
                reasoning=("Automated evaluation unavailable: LLM response missing text content."),
                **provenance,
            )
        )
        return EvaluatorOutcome(
            evaluation=evaluation,
            langfuse_evaluation_id=langfuse_evaluation_id,
        )

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        _logger.warning(
            "task_outcome_evaluator: invalid JSON for run=%s: %s; raw=%r",
            task_run.id,
            exc,
            text[:200],
        )
        evaluation = store.create_evaluation(
            CreateTaskEvaluationInput(
                task_run_id=task_run.id,
                evaluator_type="llm",
                verdict="inconclusive",
                reasoning=(
                    f"Automated evaluation unavailable: LLM response "
                    f"was not valid JSON ({exc.msg}). Human review still "
                    f"required."
                ),
                **provenance,
            )
        )
        return EvaluatorOutcome(
            evaluation=evaluation,
            langfuse_evaluation_id=langfuse_evaluation_id,
        )

    try:
        normalised = _validate_evaluator_payload(payload)
    except ValueError as exc:
        _logger.warning(
            "task_outcome_evaluator: schema violation for run=%s: %s",
            task_run.id,
            exc,
        )
        evaluation = store.create_evaluation(
            CreateTaskEvaluationInput(
                task_run_id=task_run.id,
                evaluator_type="llm",
                verdict="inconclusive",
                reasoning=(
                    f"Automated evaluation unavailable: LLM response "
                    f"failed schema validation ({exc})."
                ),
                **provenance,
            )
        )
        return EvaluatorOutcome(
            evaluation=evaluation,
            langfuse_evaluation_id=langfuse_evaluation_id,
        )

    evaluation = store.create_evaluation(
        CreateTaskEvaluationInput(
            task_run_id=task_run.id,
            evaluator_type="llm",
            verdict=normalised["verdict"],
            confidence=normalised["confidence"],
            quality_score=normalised["quality"],
            proposed_task_family=normalised["task_family"],
            reasoning=normalised["reasoning"],
            evidence=normalised["evidence"],
            unresolved_issues=normalised["unresolved_issues"],
            **provenance,
        )
    )
    return EvaluatorOutcome(
        evaluation=evaluation,
        langfuse_evaluation_id=langfuse_evaluation_id,
    )


__all__ = [
    "EVALUATOR_ACCURACY_VALUES",
    "EVALUATOR_JSON_SCHEMA",
    "EvaluatorEvidence",
    "EvaluatorOutcome",
    "build_evaluator_prompt",
    "collect_evidence",
    "evaluate_task_outcome",
]
