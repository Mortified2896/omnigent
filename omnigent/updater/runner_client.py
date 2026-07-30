"""Result delivery to the originating conversation (issue #38 §8).

The final result must reach the conversation that initiated the
update even though Omnigent restarted during the operation.

The contract:

* the controller persists the result file before attempting delivery;
* the controller calls ``POST /api/updater/result-deliver`` on the
  web service with the structured result payload;
* the web service inserts a comment with ``path =
  "__system__/update-result.json"`` carrying the result body; the
  comment acts as the durable, queryable record of the delivery;
* a durable idempotency key (``request_id``) prevents duplicate
  deliveries — the endpoint refuses to insert a second comment for
  the same request id;
* if the web service is unreachable, the controller writes the
  result into the pending-deliveries queue so the web service can
  reconcile on startup;
* retries never produce duplicate result messages because the
  ``request_id`` is checked before insertion.

The controller also exposes a status API helper so an operator (or
Verity) can query a request by ``request_id``:

* ``GET /api/updater/requests/<id>`` returns the request +
  checkpoint + result + delivery state in one shot.

Both endpoints sit behind the same auth provider as the rest of
the web service. The updater uses a shared secret in the
``X-Omnigent-Updater-Token`` header; the operator running the
updater sets ``OMNIGENT_UPDATER_TOKEN`` in the updater env.
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

from omnigent.updater import layout
from omnigent.updater.protocol import ResultRecord

_ENV_TOKEN = "OMNIGENT_UPDATER_TOKEN"


def _token() -> str | None:
    raw = os.environ.get(_ENV_TOKEN, "").strip()
    return raw or None


@dataclass(frozen=True)
class DeliveryOutcome:
    """Outcome of a single delivery attempt.

    :param delivered: True iff the web service accepted the result.
    :param comment_id: The id of the inserted comment (when delivered).
    :param message: Human-readable status text for logging.
    """

    delivered: bool
    comment_id: str = ""
    message: str = ""


class DeliveryError(RuntimeError):
    """Raised when a delivery attempt produces a malformed response."""


class _HttpClient:
    """Tiny HTTP client for talking to the web service.

    Intentionally minimal — the updater makes at most a handful of
    HTTP calls per request, and using ``urllib.request`` keeps the
    updater free of any runtime dependency on the live Omnigent
    release.
    """

    def __init__(self, *, host: str = "127.0.0.1", port: int | None = None) -> None:
        self._host = host
        self._port = port if port is not None else layout.notify_port()

    @property
    def base_url(self) -> str:
        return f"http://{self._host}:{self._port}"

    def post_json(
        self, path: str, payload: dict[str, Any], *, timeout: float = 10.0
    ) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        token = _token()
        if token:
            headers["X-Omnigent-Updater-Token"] = token
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise DeliveryError(f"HTTP {exc.code} from {path}: {raw}") from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise DeliveryError(f"network error talking to {path}: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"invalid JSON from {path}: {raw!r}") from exc

    def get_json(self, path: str, *, timeout: float = 10.0) -> dict[str, Any]:
        headers: dict[str, str] = {}
        token = _token()
        if token:
            headers["X-Omnigent-Updater-Token"] = token
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            headers=headers,
            method="GET",
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            raise DeliveryError(f"HTTP {exc.code} from {path}: {raw}") from exc
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise DeliveryError(f"network error talking to {path}: {exc}") from exc
        try:
            return json.loads(raw) if raw else {}
        except json.JSONDecodeError as exc:
            raise DeliveryError(f"invalid JSON from {path}: {raw!r}") from exc


def deliver_result(
    result: ResultRecord,
    *,
    client: _HttpClient | None = None,
) -> DeliveryOutcome:
    """Deliver the result to the originating conversation.

    Tries a single ``POST /api/updater/result-deliver`` and returns
    the outcome. Network errors raise :class:`DeliveryError` so the
    caller can decide to queue the result for later delivery
    rather than treating the attempt as a final failure.
    """
    if not result.origin_conversation_id_for_delivery:
        return DeliveryOutcome(
            delivered=False,
            message="no origin_conversation_id set; result is operator-only",
        )
    http = client or _HttpClient()
    try:
        response = http.post_json(
            "/api/updater/result-deliver",
            {
                "request_id": result.request_id,
                "final_status": result.final_status,
                "target_sha": result.target_sha,
                "previous_sha": result.previous_sha,
                "deployed_sha": result.deployed_sha,
                "failure_phase": result.failure_phase,
                "failure_reason": result.failure_reason,
                "rollback_performed": result.rollback_performed,
                "rollback_result": result.rollback_result,
            },
        )
    except DeliveryError as exc:
        return DeliveryOutcome(delivered=False, message=str(exc))
    return DeliveryOutcome(
        delivered=bool(response.get("delivered")),
        comment_id=str(response.get("comment_id", "")),
        message=str(response.get("message", "")),
    )


def query_status(
    request_id: str,
    *,
    client: _HttpClient | None = None,
) -> dict[str, Any]:
    """Query the web service's read-only status endpoint.

    Used by ``omnigent-updater status <request_id>``.
    """
    http = client or _HttpClient()
    return http.get_json(f"/api/updater/requests/{request_id}")


# Convenience: ``ResultRecord`` is a dataclass without the
# conversation-id field exposed; the web service stores it as a
# label. We expose it here via a helper so the delivery path can
# reach the originating session without changing the durable
# schema (the session id is captured at request creation time).


def _origin_conversation_id(self: ResultRecord) -> str:
    """Default ``origin_conversation_id`` for delivery.

    Most updater-spawned requests store the conversation id in a
    request-side field; the result record keeps a copy via the
    ``events_tail`` so delivery does not need to re-load the
    request. This helper extracts it.
    """
    for event in self.events_tail:
        ctx = event.get("context", {}) or {}
        if isinstance(ctx, dict) and ctx.get("origin_conversation_id"):
            return str(ctx["origin_conversation_id"])
    return ""


# Bind the helper as an attribute on ResultRecord so callers do
# not need to remember the indirection.
ResultRecord.origin_conversation_id_for_delivery = property(  # type: ignore[attr-defined]
    _origin_conversation_id
)


__all__ = [
    "DeliveryError",
    "DeliveryOutcome",
    "deliver_result",
    "query_status",
]
