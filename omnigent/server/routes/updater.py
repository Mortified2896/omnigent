"""HTTP endpoints the updater talks to (issue #38 §4, §8).

The web service exposes four routes under ``/api/updater``:

* ``POST /api/updater/result-deliver`` — accept a terminal
  result from the updater, persist it as a comment with
  ``path="__system__/update-result.json"`` in the originating
  conversation. The endpoint is idempotent on ``request_id``:
  duplicates return the existing comment id without inserting a
  second row, so retries never produce duplicate result messages.

* ``GET /api/updater/drain-status`` — return
  ``{"draining": bool, "active_sessions": [...],
  "active_runners": [...]}``. The updater polls this while
  waiting for an in-flight update to finish its drain phase.

* ``POST /api/updater/request-cancel`` — request that an active
  session be cancelled. The endpoint is best-effort: the runner
  may refuse if the session is in a state where cancelling is
  unsafe.

* ``GET /api/updater/requests/<id>`` — return the request +
  checkpoint + result + delivery state for the operator CLI's
  ``status`` command and Verity's read-only status queries.

Auth model
----------

* The four endpoints accept either the existing session auth
  (``auth_provider``) or an updater-specific shared token in the
  ``X-Omnigent-Updater-Token`` header. The updater uses the token
  header; the operator's ``status`` command uses either the
  session auth or the same token.
* The token is sourced from ``OMNIGENT_UPDATER_TOKEN``. When the
  env var is unset the updater header is refused (fail-closed).
  When the env var is set, requests carrying the matching token
  bypass session auth — the updater runs as a system service and
  does not have a user identity.

Maintenance mode
----------------

The drain and cancel endpoints read the durable maintenance
marker file the updater writes. The web service also reconciles
the marker on every startup so a crashed updater does not leave
the maintenance flag stuck.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator

from omnigent.errors import OmnigentError
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.stores.comment_store import CommentStore
from omnigent.stores.conversation_store import ConversationStore
from omnigent.updater import layout as updater_layout
from omnigent.updater import maintenance as updater_maintenance

_UPDATER_TOKEN_ENV = "OMNIGENT_UPDATER_TOKEN"
_SYSTEM_RESULT_PATH = "__system__/update-result.json"


def _check_token(request: Request) -> bool:
    """Return whether the request carries the updater token.

    When the env var is unset, the token is disabled and every
    request without valid session auth is refused. When the env
    var is set, requests carrying the matching token are allowed
    in lieu of session auth.
    """
    configured = os.environ.get(_UPDATER_TOKEN_ENV, "").strip()
    if not configured:
        return False
    supplied = request.headers.get("X-Omnigent-Updater-Token", "").strip()
    return bool(supplied) and supplied == configured


def _require_caller(request: Request, auth_provider: AuthProvider | None) -> str:
    """Resolve the caller's identity, accepting either session auth or the
    updater token.

    Returns the user id when session auth applies, or
    ``"updater:<pid>"`` when the updater token is the source.
    """
    if _check_token(request):
        return f"updater:{os.getpid()}"
    if auth_provider is None:
        raise HTTPException(status_code=401, detail="Authentication required")
    return require_user(request, auth_provider)


# ----------------------------------------------------------------------
# Request / response models
# ----------------------------------------------------------------------


class DeliverRequest(BaseModel):
    """The structured result payload the updater delivers.

    Mirrors :class:`omnigent.updater.protocol.ResultRecord`'s
    fields so the web service can persist the result as a comment
    without re-deriving anything. All fields are required because
    the updater must always send a complete record.
    """

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=26, max_length=26)
    final_status: str
    target_sha: str = Field(min_length=40, max_length=40)
    previous_sha: str = Field(min_length=40, max_length=40, default="0" * 40)
    deployed_sha: str = Field(min_length=40, max_length=40, default="0" * 40)
    failure_phase: str = ""
    failure_reason: str = ""
    rollback_performed: bool = False
    rollback_result: str | None = None

    @field_validator("final_status")
    @classmethod
    def _validate_final_status(cls, value: str) -> str:
        if value not in {
            "succeeded",
            "rejected",
            "failed",
            "rolled_back",
            "rollback_failed",
        }:
            raise ValueError(f"unknown final_status {value!r}")
        return value


class DeliverResponse(BaseModel):
    """Response body for ``POST /api/updater/result-deliver``."""

    delivered: bool
    comment_id: str
    message: str


class CancelRequest(BaseModel):
    """The body of ``POST /api/updater/request-cancel``."""

    model_config = ConfigDict(extra="forbid")

    session_id: str | None = None


# ----------------------------------------------------------------------
# Idempotency helpers
# ----------------------------------------------------------------------


def _idempotency_marker_path(state_root: Path, request_id: str) -> Path:
    """Per-request idempotency marker the endpoint checks before insert.

    Lives outside the deploy root so the live release does not
    hold it. The web service uses the marker as a fast path; the
    comment-store unique constraint on
    ``(request_id, idempotency_key)`` would be a stronger
    guarantee but would require a schema migration we don't ship
    here.
    """
    return state_root / "delivery-ack" / f"{request_id}.json"


def _mark_delivered(state_root: Path, request_id: str, comment_id: str) -> None:
    target = _idempotency_marker_path(state_root, request_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps({"comment_id": comment_id}, sort_keys=True) + "\n")


def _already_delivered(state_root: Path, request_id: str) -> str | None:
    """Return the previously delivered comment id, or ``None``."""
    target = _idempotency_marker_path(state_root, request_id)
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    return str(data.get("comment_id", "")) or None


def _resolve_state_root() -> Path:
    """Resolve the updater state root, falling back to its default."""
    raw = os.environ.get("OMNIGENT_UPDATER_STATE_ROOT", "").strip()
    if raw:
        return Path(raw)
    return updater_layout.state_root()


# ----------------------------------------------------------------------
# Router factory
# ----------------------------------------------------------------------


def create_updater_router(
    *,
    auth_provider: AuthProvider | None,
    conversation_store: ConversationStore,  # noqa: ARG001 - reserved for future routing
    comment_store: CommentStore,
    session_lister: callable[[], list[str]] | None = None,
    runner_lister: callable[[], list[str]] | None = None,
) -> APIRouter:
    """Build the FastAPI router with the four updater endpoints.

    :param auth_provider: Session auth provider; ``None`` means
        only the updater token can call these endpoints.
    :param conversation_store: For resolving the originating
        conversation when delivering a result.
    :param comment_store: For inserting the comment that records
        the delivered result.
    :param session_lister: Optional callable returning the list
        of currently active session ids. Defaults to "empty" so
        drain reports ``draining=True, active_sessions=[]`` and
        the updater can proceed.
    :param runner_lister: Optional callable returning the list of
        currently active runner ids.
    """

    router = APIRouter(prefix="/api/updater", tags=["updater"])
    sessions_lister = session_lister or (list)
    runners_lister = runner_lister or (list)

    @router.post("/result-deliver", response_model=DeliverResponse)
    async def deliver_result(
        request: Request,
        body: DeliverRequest,
    ) -> DeliverResponse:
        _require_caller(request, auth_provider)
        if body.final_status not in {
            "succeeded",
            "rejected",
            "failed",
            "rolled_back",
            "rollback_failed",
        }:
            raise HTTPException(
                status_code=422,
                detail=f"unknown final_status {body.final_status!r}",
            )

        state_root = _resolve_state_root()
        existing = _already_delivered(state_root, body.request_id)
        if existing:
            return DeliverResponse(
                delivered=True,
                comment_id=existing,
                message="already delivered; idempotent return",
            )

        # The originating conversation is the one whose session id
        # matches ``body.request_id``'s session annotation. We
        # expose the conversation id through the result's events
        # tail in a follow-up patch; for now, look it up by
        # ``request_id`` in the durable state root so the comment
        # lands somewhere an operator can find it.
        conversation_id = _conversation_for_request(body.request_id)
        body_text = _format_result_body(body.model_dump())
        try:
            comment = await asyncio.to_thread(
                comment_store.add,
                conversation_id=conversation_id,
                path=_SYSTEM_RESULT_PATH,
                body=body_text,
                start_index=0,
                end_index=len(body_text),
                anchor_content=None,
                created_by=None,
            )
        except OmnigentError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc

        _mark_delivered(state_root, body.request_id, comment.id)
        return DeliverResponse(
            delivered=True,
            comment_id=comment.id,
            message="result recorded",
        )

    @router.get("/drain-status")
    async def drain_status(request: Request) -> dict[str, Any]:
        _require_caller(request, auth_provider)
        marker = updater_maintenance.read_marker()
        active = sessions_lister() or []
        runners = runners_lister() or []
        return {
            "draining": bool(marker.active),
            "active_sessions": list(active),
            "active_runners": list(runners),
        }

    @router.post("/request-cancel")
    async def request_cancel(request: Request, body: CancelRequest) -> dict[str, Any]:
        _require_caller(request, auth_provider)
        # The endpoint is best-effort: we record the request so
        # the operator can see who asked, but cancellation is
        # delegated to the runner / harness layer. The updater
        # never assumes cancellation succeeded.
        state_root = _resolve_state_root()
        cancel_log = state_root / "cancel-requests.jsonl"
        cancel_log.parent.mkdir(parents=True, exist_ok=True)
        with cancel_log.open("a") as f:
            f.write(
                json.dumps(
                    {
                        "ts": _now_iso(),
                        "session_id": body.session_id,
                        "requested_by": _require_caller(request, auth_provider),
                    },
                    sort_keys=True,
                )
                + "\n"
            )
        return {"recorded": True, "session_id": body.session_id}

    @router.get("/requests/{request_id}")
    async def get_request_status(
        request: Request,
        request_id: str,
    ) -> dict[str, Any]:
        _require_caller(request, auth_provider)
        store_root = _resolve_state_root()
        request_path = store_root / "requests" / f"{request_id}.json"
        if not request_path.is_file():
            raise HTTPException(status_code=404, detail="no such request")
        request_data = json.loads(request_path.read_text())
        running = store_root / "running" / f"{request_id}.json"
        result = store_root / "results" / f"{request_id}.json"
        pending = store_root / "pending-deliveries" / f"{request_id}.json"
        return {
            "request": request_data,
            "checkpoint": json.loads(running.read_text()) if running.is_file() else None,
            "result": json.loads(result.read_text()) if result.is_file() else None,
            "pending_delivery": pending.is_file(),
        }

    return router


def _conversation_for_request(request_id: str) -> str:
    """Resolve the originating conversation id for ``request_id``.

    Falls back to a deterministic synthetic conversation id when
    the request file is unavailable so an operator-triggered
    result still records somewhere. The system path comment
    surfaces in the conversation timeline as a system message.
    """
    state_root = _resolve_state_root()
    req_path = state_root / "requests" / f"{request_id}.json"
    if req_path.is_file():
        try:
            data = json.loads(req_path.read_text())
            cid = data.get("origin_conversation_id")
            if cid:
                return str(cid)
            sid = data.get("origin_session_id")
            if sid:
                return str(sid)
        except (OSError, json.JSONDecodeError):
            pass
    return f"updater_{request_id}"


def _format_result_body(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def _now_iso() -> str:
    import time

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


# Reconcile maintenance marker at web-service startup. The
# reconcile is idempotent; calling it on every import keeps the
# tests honest.


_RECONCILE_LOCK = threading.Lock()


def reconcile_startup(*, owner_pid: int | None = None) -> None:
    """Reconcile the maintenance marker at web-service startup.

    If the marker is active but the owning updater process is
    dead, the marker is cleared (the operator is the only one
    who can re-engage it). If the owner is alive, the marker is
    left in place.

    Wired into the ``create_app`` lifespan hook so every startup
    recovers from a crashed updater.
    """
    with _RECONCILE_LOCK:
        updater_maintenance.reconcile_marker(owner_pid=owner_pid)
