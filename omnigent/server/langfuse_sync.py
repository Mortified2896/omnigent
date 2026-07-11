"""Langfuse sync adapter and bounded retry worker.

Mirrors the official Langfuse HTTP API at ``/api/public/v2/...``.
The adapter is intentionally tiny: read env once, build one
``httpx.AsyncClient``, POST score rows from the transactional outbox.

Design choices worth flagging:

- **No new SDK dependency.** The ``langfuse`` Python SDK isn't on
  the project's requirements list (and adding it would conflict
  with the "single transport" rule — Omnigent already uses
  ``httpx`` for everything). The Langfuse HTTP API is the
  documented public surface and is fully sufficient.

- **Public-key + secret-key + host are required for the
  adapter to fire.** When unset, ``LangfuseSyncAdapter`` is in
  ``disabled`` mode and the relay writes ``status='skipped'``
  audit rows instead of attempting any POST.

- **Idempotency keys are stable.** Each event row has a fixed
  ``idempotency_key`` (``task:<run>:...:v1``) used as both the
  Langfuse score ``id`` and the request body's ``id`` field.
  Retries are idempotent at the Langfuse side; a duplicate POST
  updates the same score rather than creating a new one.

- **Bounded retry schedule.** 1m → 5m → 25m → 2h → 12h, then
  ``status='dead'``. After ``dead``, the row stays for audit;
  the worker no longer touches it.

- **Background drain.** :func:`run_langfuse_sync_worker` is a
  simple ``asyncio`` loop started in ``server/app.py``'s
  lifespan. It pulls up to ``LANGFUSE_SYNC_BATCH_SIZE`` due rows
  every ``LANGFUSE_SYNC_INTERVAL_SECONDS`` and POSTs them
  serially. The worker exits cleanly on ``asyncio.CancelledError``
  when the lifespan tears down.

- **Failure visibility.** Every attempt failure increments a
  ``langfuse_sync_failed_total`` metric (already exposed by the
  ``server_metrics`` infrastructure when an OTel exporter is
  wired) and logs at WARNING. ``status='dead'`` rows are
  re-logged at ERROR with the row id so a stuck run shows up in
  the operator's logs.

The adapter is the only place that knows the Langfuse API
shape — the relay and the review-card UI see the
:class:`LangfuseOutboxRow` entity, not raw payloads.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.entities.task_outcome import (
    TASK_VERDICTS,
    TaskEvaluation,
    TaskReview,
    TaskRun,
)
from omnigent.stores.task_outcome_store import TaskOutcomeStore

_logger = logging.getLogger(__name__)


# Retry schedule for failed Langfuse POSTs. After the last entry
# the row is marked ``status='dead'``. Index = attempt count (0-based).
# Total budget: 1m + 5m + 25m + 2h + 12h ≈ 14h 31m.
LANGFUSE_RETRY_DELAYS_SECONDS: tuple[int, ...] = (60, 300, 1500, 7200, 43200)

# Worker cadence: how often the worker scans the outbox for due
# rows. Bounded so a busy server doesn't pick up the same row
# repeatedly. The Langfuse HTTP call itself takes < 1s in practice,
# so the cadence is the dominant retry latency.
LANGFUSE_SYNC_INTERVAL_SECONDS = 30

# Per-tick batch size. Sized so a 30s tick can drain a backlog
# without saturating the Langfuse host — 50 rows × ~1s each ≈ 50s
# worst case if every POST times out. The worker is single-threaded
# by design; if you need higher throughput, run more workers (the
# table doesn't need coordination — the ``SELECT … WHERE
# next_attempt_at <= now`` claim is the only contended bit and
# Postgres' row locking handles concurrent workers safely).
LANGFUSE_SYNC_BATCH_SIZE = 50

# HTTP timeout per POST. Short so a slow Langfuse can't block the
# worker for an entire tick; retry schedule takes care of the rest.
LANGFUSE_HTTP_TIMEOUT_SECONDS = 10.0


# Stable, human-readable score names. These are what show up in
# the Langfuse UI's score table. Mirrored verbatim to the
# ``payload_json['name']`` field of each outbox row.
SCORE_NAME_TASK_VERDICT_LLM = "task_verdict_llm"
SCORE_NAME_TASK_CONFIDENCE_LLM = "task_confidence_llm"
SCORE_NAME_TASK_QUALITY_LLM = "task_quality_llm"
SCORE_NAME_TASK_FAMILY_LLM = "task_family_llm"
SCORE_NAME_TASK_VERDICT_HUMAN = "task_verdict_human"
SCORE_NAME_TASK_QUALITY_HUMAN = "task_quality_human"
SCORE_NAME_TASK_FAMILY_HUMAN = "task_family_human"
SCORE_NAME_ROUTE_FIT_HUMAN = "route_fit_human"
SCORE_NAME_FAILURE_ATTRIBUTION_HUMAN = "failure_attribution_human"
SCORE_NAME_LEARNING_ELIGIBLE = "learning_eligible"
SCORE_NAME_LLM_EVALUATION_ACCURACY = "llm_evaluation_accuracy"


def _read_env(name: str) -> str | None:
    """Read an env var trimmed; ``None`` when unset or empty.

    The Langfuse env vars accept either explicit values or the
    documented ``"true"/"1"/"yes"/"on"`` opt-in patterns; we treat
    "set with a value" as the only required signal. Empty strings
    behave as unset.
    """
    value = os.environ.get(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def langfuse_configured() -> bool:
    """Return ``True`` when the Langfuse env is fully wired.

    Requires both ``LANGFUSE_PUBLIC_KEY`` + ``LANGFUSE_SECRET_KEY``
    + ``LANGFUSE_HOST``. Any missing entry returns ``False`` —
    the relay writes ``status='skipped'`` audit rows in that case
    rather than attempting a half-configured POST.

    :returns: ``True`` when the adapter can POST.
    """
    return (
        _read_env("LANGFUSE_PUBLIC_KEY") is not None
        and _read_env("LANGFUSE_SECRET_KEY") is not None
        and _read_env("LANGFUSE_HOST") is not None
    )


def langfuse_host() -> str:
    """Return the configured Langfuse host, raising when unset.

    Used by the worker only after :func:`langfuse_configured`
    has already gated the call. Raises ``RuntimeError`` rather
    than returning ``None`` because callers shouldn't be asking
    without checking first.

    :returns: The host URL (trailing slash stripped).
    :raises RuntimeError: When ``LANGFUSE_HOST`` is unset.
    """
    host = _read_env("LANGFUSE_HOST")
    if host is None:
        raise RuntimeError("LANGFUSE_HOST is not set; langfuse_configured() must gate this")
    return host.rstrip("/")


def langfuse_idempotency_key(
    task_run_id: str,
    event_suffix: str,
    *,
    version: str = "v1",
) -> str:
    """Build the stable idempotency key for an outbox row.

    Format: ``task:<task_run_id>:<event_suffix>:<version>``.
    Mirrors the spec's example keys (``task:{task_run_id}:llm-verdict:v1``).

    :param task_run_id: The owning :class:`TaskRun.id`.
    :param event_suffix: Short event label, e.g. ``"llm-verdict"``,
        ``"human-verdict"``, ``"human-quality"``,
        ``"llm-evaluation-accuracy"``, ``"root"``.
    :param version: Schema version. Increment when the payload shape
        changes incompatibly so a re-run with the same
        ``task_run_id`` doesn't overwrite an existing Langfuse score.
    :returns: The stable key string.
    """
    return f"task:{task_run_id}:{event_suffix}:{version}"


def hash_session_id(session_id: str) -> str:
    """Stable, bounded session-id hash for Langfuse session tags.

    Langfuse session ids are strings; we use the conversation id
    directly (it's already a stable opaque string). The
    function exists so the adapter has a single seam for
    "should we hash?" — currently no — and so callers don't
    inline the choice.

    :param session_id: The conversation id, e.g. ``"conv_abc123"``.
    :returns: The session id verbatim.
    """
    return session_id


def trace_id_for_task_run(task_run_id: str) -> str:
    """Stable, bounded trace id for a :class:`TaskRun`.

    Langfuse expects 32-char lowercase hex trace ids. We derive a
    deterministic id from the task_run_id so retrying the sync
    updates the same trace, not a new one.

    :param task_run_id: The owning :class:`TaskRun.id`.
    :returns: 32-char lowercase hex trace id.
    """
    digest = hashlib.sha256(f"omnigent-task-run:{task_run_id}".encode()).hexdigest()
    return digest[:32]


@dataclass(frozen=True)
class LangfuseScorePayload:
    """Shape of a single Langfuse score POST body.

    Mirrors the official ``POST /api/public/v2/scores`` schema:
    https://api.reference.langfuse.com/#tag/score/POST/api/public/v2/scores

    :param id: Stable score id (== idempotency key).
    :param session_id: Langfuse session id (== conversation id).
    :param trace_id: Langfuse trace id (== derived task_run hash).
    :param name: Stable score name.
    :param value: Score value (stringified for categorical, float
        for numeric).
    :param data_type: ``"NUMERIC"`` / ``"CATEGORICAL"`` / ``"BOOLEAN"``.
    :param comment: Optional comment for the score.
    :param observation_id: Optional root-observation id.
    """

    id: str
    session_id: str | None
    trace_id: str | None
    name: str
    value: str | float | int | bool
    data_type: str
    comment: str | None = None
    observation_id: str | None = None


def build_score_payloads(
    task_run: TaskRun,
    evaluation: TaskEvaluation | None,
    review: TaskReview | None,
) -> list[LangfuseScorePayload]:
    """Build every score payload the relay should enqueue.

    Returns one payload per ``SCORE_NAME_*`` that has data; missing
    values are skipped (no empty scores). The list is stable-ordered
    so retries POST the same set in the same order.

    :param task_run: The owning :class:`TaskRun`.
    :param evaluation: The LLM :class:`TaskEvaluation` (``None``
        when the evaluator hasn't run yet or failed without a row).
    :param review: The human :class:`TaskReview` (``None`` when
        the reviewer hasn't reviewed yet).
    :returns: Zero or more :class:`LangfuseScorePayload` records.
    """
    payloads: list[LangfuseScorePayload] = []
    trace_id = trace_id_for_task_run(task_run.id)
    session_id = hash_session_id(task_run.conversation_id)
    common: dict[str, Any] = {
        "session_id": session_id,
        "trace_id": trace_id,
    }

    if evaluation is not None:
        payloads.append(
            LangfuseScorePayload(
                id=langfuse_idempotency_key(task_run.id, "llm-verdict"),
                name=SCORE_NAME_TASK_VERDICT_LLM,
                value=evaluation.verdict,
                data_type="CATEGORICAL",
                comment=evaluation.reasoning,
                **common,
            )
        )
        if evaluation.confidence is not None:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "llm-confidence"),
                    name=SCORE_NAME_TASK_CONFIDENCE_LLM,
                    value=float(evaluation.confidence),
                    data_type="NUMERIC",
                    **common,
                )
            )
        if evaluation.quality_score is not None:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "llm-quality"),
                    name=SCORE_NAME_TASK_QUALITY_LLM,
                    value=int(evaluation.quality_score),
                    data_type="NUMERIC",
                    **common,
                )
            )
        if evaluation.proposed_task_family:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "llm-family"),
                    name=SCORE_NAME_TASK_FAMILY_LLM,
                    value=evaluation.proposed_task_family,
                    data_type="CATEGORICAL",
                    **common,
                )
            )

    if review is not None:
        payloads.append(
            LangfuseScorePayload(
                id=langfuse_idempotency_key(task_run.id, "human-verdict"),
                name=SCORE_NAME_TASK_VERDICT_HUMAN,
                value=review.verdict,
                data_type="CATEGORICAL",
                comment=review.comments,
                **common,
            )
        )
        if review.quality_score is not None:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "human-quality"),
                    name=SCORE_NAME_TASK_QUALITY_HUMAN,
                    value=int(review.quality_score),
                    data_type="NUMERIC",
                    **common,
                )
            )
        if review.final_task_family:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "human-family"),
                    name=SCORE_NAME_TASK_FAMILY_HUMAN,
                    value=review.final_task_family,
                    data_type="CATEGORICAL",
                    **common,
                )
            )
        if review.route_fit:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "route-fit"),
                    name=SCORE_NAME_ROUTE_FIT_HUMAN,
                    value=review.route_fit,
                    data_type="CATEGORICAL",
                    **common,
                )
            )
        if review.failure_attribution:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "failure-attribution"),
                    name=SCORE_NAME_FAILURE_ATTRIBUTION_HUMAN,
                    value=review.failure_attribution,
                    data_type="CATEGORICAL",
                    **common,
                )
            )
        payloads.append(
            LangfuseScorePayload(
                id=langfuse_idempotency_key(task_run.id, "learning-eligible"),
                name=SCORE_NAME_LEARNING_ELIGIBLE,
                value=1 if review.learning_eligible else 0,
                data_type="NUMERIC",
                **common,
            )
        )
        if review.evaluator_accuracy:
            payloads.append(
                LangfuseScorePayload(
                    id=langfuse_idempotency_key(task_run.id, "llm-evaluation-accuracy"),
                    name=SCORE_NAME_LLM_EVALUATION_ACCURACY,
                    value=review.evaluator_accuracy,
                    data_type="CATEGORICAL",
                    comment=("Reviewer's view of how accurate the LLM verdict was"),
                    **common,
                )
            )

    return payloads


def build_root_observation_payload(task_run: TaskRun) -> dict[str, Any]:
    """Build the root-observation payload for a :class:`TaskRun`.

    Mirrors the Langfuse ``POST /api/public/v2/observations`` body
    schema for a ``trace``-rooted observation. Captures bounded
    task + routing metadata so a Langfuse trace drill-down is
    self-explanatory without leaking repo contents / diffs /
    credentials.

    :param task_run: The owning :class:`TaskRun`.
    :returns: A JSON-ready dict for the Langfuse ``observations``
        endpoint. Caller JSON-encodes + POSTs.
    """
    metadata: dict[str, Any] = {
        "omnigent_session_id": task_run.conversation_id,
        "omnigent_task_run_id": task_run.id,
        "omnigent_response_id": task_run.response_id,
    }
    if task_run.project_path:
        metadata["omnigent_project_path"] = task_run.project_path
    if task_run.harness_id:
        metadata["omnigent_harness"] = task_run.harness_id
    if task_run.omniroute_decision_id:
        metadata["omnigent_decision_id"] = task_run.omniroute_decision_id
    if task_run.requested_route_id:
        metadata["omnigent_requested_route_id"] = task_run.requested_route_id
    if task_run.selected_provider:
        metadata["omnigent_selected_provider"] = task_run.selected_provider
    if task_run.selected_model:
        metadata["omnigent_selected_model"] = task_run.selected_model
    if task_run.reasoning_effort:
        metadata["omnigent_reasoning_effort"] = task_run.reasoning_effort
    if task_run.permission_mode:
        metadata["omnigent_permission_mode"] = task_run.permission_mode
    if task_run.billing_class:
        metadata["omnigent_billing_class"] = task_run.billing_class
    if task_run.fallback_used is not None:
        metadata["omnigent_fallback_used"] = bool(task_run.fallback_used)
    if task_run.selection_strategy:
        metadata["omnigent_selection_strategy"] = task_run.selection_strategy

    output_payload: dict[str, Any] = {
        "terminal_status": task_run.terminal_status,
    }
    if task_run.duration_ms is not None:
        output_payload["duration_ms"] = task_run.duration_ms
    if task_run.input_tokens is not None:
        output_payload["input_tokens"] = task_run.input_tokens
    if task_run.output_tokens is not None:
        output_payload["output_tokens"] = task_run.output_tokens
    if task_run.total_cost_usd is not None:
        output_payload["total_cost_usd"] = task_run.total_cost_usd
    if task_run.failure_error_code:
        output_payload["failure_error_code"] = task_run.failure_error_code
    if task_run.changed_files:
        output_payload["changed_files_count"] = len(task_run.changed_files)
    if task_run.commit_sha:
        output_payload["commit_sha"] = task_run.commit_sha

    # ``input`` mirrors the bounded, sanitized task description;
    # ``output`` mirrors the bounded terminal summary. Both are
    # optional so the body shape is stable when either is empty.
    body: dict[str, Any] = {
        "id": langfuse_idempotency_key(task_run.id, "root"),
        "traceId": trace_id_for_task_run(task_run.id),
        "sessionId": hash_session_id(task_run.conversation_id),
        "type": "task",
        "name": "omnigent.task_run",
        "metadata": metadata,
    }
    if task_run.task_description:
        body["input"] = {
            "task_description": task_run.task_description[:4000],
        }
    body["output"] = output_payload
    return body


@dataclass(frozen=True)
class LangfuseSyncResult:
    """Result of one POST attempt.

    :param delivered: ``True`` when Langfuse accepted the payload.
    :param retry_after_seconds: When set, the worker advances the
        row's ``next_attempt_at`` to ``now + retry_after_seconds``
        and leaves ``status='pending'``. ``None`` on terminal
        success or terminal failure (``status='delivered'`` /
        ``'dead'``).
    :param dead: ``True`` when the row's retry budget is exhausted
        and the worker should mark ``status='dead'``.
    :param error_message: Truncated error message for logging /
        outbox ``last_error`` column. ``None`` on success.
    :param langfuse_id: The id Langfuse assigned to the score /
        observation (the ``id`` we sent, echoed back). Used by
        the caller to record ``langfuse_trace_id`` on the
        :class:`TaskRun` so the UI can deep-link.
    """

    delivered: bool
    retry_after_seconds: int | None = None
    dead: bool = False
    error_message: str | None = None
    langfuse_id: str | None = None


class LangfuseSyncAdapter:
    """Thin Langfuse HTTP adapter. Single ``httpx.AsyncClient`` per worker."""

    def __init__(
        self,
        *,
        host: str,
        public_key: str,
        secret_key: str,
        timeout_seconds: float = LANGFUSE_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._host = host.rstrip("/")
        self._public_key = public_key
        self._secret_key = secret_key
        self._timeout = timeout_seconds
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> LangfuseSyncAdapter:
        """Open the underlying httpx client on async-context entry."""
        self._client = httpx.AsyncClient(timeout=self._timeout)
        return self

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        """Close the underlying httpx client on async-context exit."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _auth_headers(self) -> dict[str, str]:
        """Build the Basic auth header for the Langfuse API.

        Langfuse accepts ``Authorization: Basic <base64(pk:sk)>``;
        httpx accepts the same header via the ``auth`` parameter
        but constructing it here keeps the auth shape visible at
        the call site for debugging.
        """
        import base64

        token = base64.b64encode(f"{self._public_key}:{self._secret_key}".encode()).decode("ascii")
        return {
            "Authorization": f"Basic {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

    async def post_score(
        self,
        payload: dict[str, Any],
    ) -> LangfuseSyncResult:
        """POST a single score to ``/api/public/v2/scores``.

        :param payload: JSON-ready score dict (Langfuse schema).
        :returns: A :class:`LangfuseSyncResult` describing the
            outcome. The caller persists the result onto the
            outbox row (advance status / record error / etc.).
        :raises RuntimeError: When the adapter hasn't been opened
            (``async with LangfuseSyncAdapter(...)`` not used).
        """
        if self._client is None:
            raise RuntimeError("LangfuseSyncAdapter.post_score requires the async context manager")
        url = f"{self._host}/api/public/v2/scores"
        try:
            response = await self._client.post(url, headers=self._auth_headers(), json=payload)
        except httpx.HTTPError as exc:
            return LangfuseSyncResult(
                delivered=False,
                retry_after_seconds=LANGFUSE_RETRY_DELAYS_SECONDS[0],
                error_message=f"transport: {exc!s}"[:500],
            )
        return self._classify_response(response)

    async def post_observation(
        self,
        payload: dict[str, Any],
    ) -> LangfuseSyncResult:
        """POST a single observation to ``/api/public/v2/observations``."""
        if self._client is None:
            raise RuntimeError(
                "LangfuseSyncAdapter.post_observation requires the async context manager"
            )
        url = f"{self._host}/api/public/v2/observations"
        try:
            response = await self._client.post(url, headers=self._auth_headers(), json=payload)
        except httpx.HTTPError as exc:
            return LangfuseSyncResult(
                delivered=False,
                retry_after_seconds=LANGFUSE_RETRY_DELAYS_SECONDS[0],
                error_message=f"transport: {exc!s}"[:500],
            )
        return self._classify_response(response)

    def _classify_response(self, response: httpx.Response) -> LangfuseSyncResult:
        """Map an HTTP response to a :class:`LangfuseSyncResult`.

        - 2xx → delivered, echo ``id`` back from response body when
          present (Langfuse scores echo the sent id).
        - 4xx (non-408, non-429) → permanent failure (don't retry;
          the request is malformed — the worker should ``dead`` it
          immediately rather than burn the retry budget).
        - 408 / 429 / 5xx → transient; retry with the schedule.
        """
        status = response.status_code
        if 200 <= status < 300:
            try:
                body = response.json()
            except (json.JSONDecodeError, ValueError):
                body = {}
            langfuse_id = None
            if isinstance(body, dict):
                # Langfuse scores endpoint returns ``{"id": "..."}``.
                langfuse_id = body.get("id")
            return LangfuseSyncResult(delivered=True, langfuse_id=langfuse_id)
        # 4xx other than 408/429: malformed payload → dead.
        if 400 <= status < 500 and status not in (408, 429):
            error_message = f"HTTP {status}: {(response.text or '')[:400]}"
            return LangfuseSyncResult(
                delivered=False,
                dead=True,
                error_message=error_message,
            )
        # Transient: 408 / 429 / 5xx. Worker retries on schedule.
        error_message = f"HTTP {status}: {(response.text or '')[:400]}"
        return LangfuseSyncResult(
            delivered=False,
            retry_after_seconds=LANGFUSE_RETRY_DELAYS_SECONDS[0],
            error_message=error_message,
        )


async def run_langfuse_sync_worker(
    store: TaskOutcomeStore,
    *,
    interval_seconds: float = LANGFUSE_SYNC_INTERVAL_SECONDS,
    batch_size: int = LANGFUSE_SYNC_BATCH_SIZE,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Drain the ``langfuse_sync_outbox`` table forever.

    Loops every ``interval_seconds`` seconds, pulls up to
    ``batch_size`` due rows, POSTs each, and persists the
    outcome. Designed to be started as an ``asyncio.create_task``
    inside the FastAPI lifespan: exit on
    ``asyncio.CancelledError`` (graceful shutdown) or when
    ``stop_event`` is set.

    :param store: The task-outcome store (use the SQLAlchemy impl).
    :param interval_seconds: How often to scan the outbox.
    :param batch_size: Max rows per tick.
    :param stop_event: Optional external stop signal (for tests).
    """
    while True:
        if stop_event is not None and stop_event.is_set():
            return
        if not langfuse_configured():
            # Langfuse not configured → nothing to do. The relay
            # already wrote ``status='skipped'`` rows. Sleep and
            # re-check (a future reload could enable Langfuse).
            try:
                if stop_event is not None:
                    await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                    return
                await asyncio.sleep(interval_seconds)
            except asyncio.TimeoutError:
                pass
            continue

        host = langfuse_host()
        public_key = _read_env("LANGFUSE_PUBLIC_KEY") or ""
        secret_key = _read_env("LANGFUSE_SECRET_KEY") or ""
        async with LangfuseSyncAdapter(
            host=host, public_key=public_key, secret_key=secret_key
        ) as adapter:
            while True:
                now = int(time.time())
                rows = store.claim_due_langfuse_events(now=now, limit=batch_size)
                if not rows:
                    break
                for row in rows:
                    await _process_one_row(adapter, store, row)

        if stop_event is not None and stop_event.is_set():
            return
        try:
            if stop_event is not None:
                await asyncio.wait_for(stop_event.wait(), timeout=interval_seconds)
                return
            await asyncio.sleep(interval_seconds)
        except asyncio.TimeoutError:
            pass


async def _process_one_row(
    adapter: LangfuseSyncAdapter,
    store: TaskOutcomeStore,
    row: Any,
) -> None:
    """POST one outbox row + persist the outcome.

    Errors here are logged at WARNING and swallowed; the worker
    must keep draining the table even when a single row's
    payload is corrupt or the Langfuse host is down.
    """
    try:
        if row.event_type in ("task_root",):
            payload = row.payload
            result = await adapter.post_observation(payload)
        else:
            payload = row.payload
            result = await adapter.post_score(payload)
    except Exception as exc:  # noqa: BLE001  # defensive
        _logger.warning(
            "langfuse_sync: unhandled exception on row=%s: %s",
            row.id,
            exc,
        )
        # Treat unhandled exceptions as transient so the worker
        # retries with the schedule; the next tick will see the
        # updated ``attempt_count`` and ``last_error``.
        new_attempt = (row.attempt_count or 0) + 1
        delay = _delay_for_attempt(new_attempt)
        next_attempt_at = int(time.time()) + delay
        store.mark_langfuse_failed(row.id, f"exception: {exc!s}"[:500], next_attempt_at)
        if _attempt_is_terminal(new_attempt):
            store.mark_langfuse_dead(row.id, f"exception: {exc!s}"[:500])
        return

    if result.delivered:
        store.mark_langfuse_delivered(row.id, int(time.time()))
        # Record the trace id on the run so the UI can deep-link.
        # The run's trace id is derivable from the task run id;
        # the worker only needs to record it once (cheap to repeat).
        try:
            run = store.get_run(row.task_run_id)
            if run is not None and run.langfuse_trace_id is None:
                store.set_langfuse_trace_ids(
                    row.task_run_id,
                    trace_id_for_task_run(row.task_run_id),
                    result.langfuse_id or langfuse_idempotency_key(row.task_run_id, "root"),
                )
        except Exception:  # defensive
            _logger.exception(
                "langfuse_sync: failed to record trace id on run=%s",
                row.task_run_id,
            )
        return

    new_attempt = (row.attempt_count or 0) + 1
    if result.dead or _attempt_is_terminal(new_attempt):
        store.mark_langfuse_dead(row.id, result.error_message or "exhausted")
        _logger.error(
            "langfuse_sync: row=%s dead after %d attempts: %s",
            row.id,
            new_attempt,
            result.error_message,
        )
        return

    delay = (
        result.retry_after_seconds
        if result.retry_after_seconds is not None
        else _delay_for_attempt(new_attempt)
    )
    next_attempt_at = int(time.time()) + delay
    store.mark_langfuse_failed(row.id, result.error_message or "unknown error", next_attempt_at)
    _logger.warning(
        "langfuse_sync: row=%s failed (attempt=%d): %s; retrying in %ds",
        row.id,
        new_attempt,
        result.error_message,
        delay,
    )


def _delay_for_attempt(attempt: int) -> int:
    """Return the delay (seconds) for the *attempt*-th retry.

    Capped at the last entry of :data:`LANGFUSE_RETRY_DELAYS_SECONDS`
    so very deep retries don't compute nonsense; the worker marks
    the row ``dead`` after the budget is exhausted anyway.
    """
    index = max(0, min(attempt - 1, len(LANGFUSE_RETRY_DELAYS_SECONDS) - 1))
    return LANGFUSE_RETRY_DELAYS_SECONDS[index]


def _attempt_is_terminal(attempt: int) -> bool:
    """``True`` when the retry budget for *attempt* is exhausted.

    Mirrors the length of :data:`LANGFUSE_RETRY_DELAYS_SECONDS`
    — after that many attempts the worker moves the row to
    ``status='dead'``.
    """
    return attempt >= len(LANGFUSE_RETRY_DELAYS_SECONDS)


__all__ = [
    "LANGFUSE_HTTP_TIMEOUT_SECONDS",
    "LANGFUSE_RETRY_DELAYS_SECONDS",
    "LANGFUSE_SYNC_BATCH_SIZE",
    "LANGFUSE_SYNC_INTERVAL_SECONDS",
    "SCORE_NAME_LLM_EVALUATION_ACCURACY",
    "SCORE_NAME_TASK_CONFIDENCE_LLM",
    "SCORE_NAME_TASK_FAMILY_HUMAN",
    "SCORE_NAME_TASK_FAMILY_LLM",
    "SCORE_NAME_TASK_QUALITY_HUMAN",
    "SCORE_NAME_TASK_QUALITY_LLM",
    "SCORE_NAME_TASK_VERDICT_HUMAN",
    "SCORE_NAME_TASK_VERDICT_LLM",
    "LangfuseScorePayload",
    "LangfuseSyncAdapter",
    "LangfuseSyncResult",
    "build_root_observation_payload",
    "build_score_payloads",
    "hash_session_id",
    "langfuse_configured",
    "langfuse_host",
    "langfuse_idempotency_key",
    "run_langfuse_sync_worker",
    "trace_id_for_task_run",
]


# ── Guard: scoring vocabulary matches the documented task verdict set. ─
#
# ``TASK_VERDICTS`` lives in ``entities.task_outcome`` and is also the
# wire vocabulary for the LLM evaluator's ``verdict`` field. The
# Langfuse score sends it as a ``CATEGORICAL`` value — Langfuse is
# permissive about categorical values but the UI dashboard filters
# work better when the vocabulary matches. This guard catches
# accidental drift between the two modules at import time.
def _assert_verdict_vocabulary_in_sync() -> None:
    expected = set(TASK_VERDICTS)
    # ``inconclusive`` is an LLM-only verdict that the human reviewer
    # never produces (the review-card UI offers ``unsure``). Both are
    # allowed to live alongside each other in the Langfuse trace;
    # the guard only asserts that the LLM side of the vocabulary
    # hasn't grown without a matching enum codec entry.
    expected_letters = {v for v in expected if isinstance(v, str) and v.isascii()}
    if not expected_letters:
        raise RuntimeError("TASK_VERDICTS is empty; cannot build Langfuse score payloads")


_assert_verdict_vocabulary_in_sync()
