"""The narrow request interface (issue #38 §2).

Omnigent must be able to request an update without receiving
arbitrary deployment privileges. The interface is a thin wrapper
around :class:`omnigent.updater.protocol.RequestRecord` that:

* accepts only the strict schema documented in
  :mod:`omnigent.updater.protocol`;
* rejects shell commands, service names, paths, remotes, scripts,
  and environment overrides (no extra fields, no ``**kwargs``);
* writes the durable request file atomically;
* returns the request id so the caller can poll status.

The implementation is callable from inside an agent (the
``sys_update_request`` builtin) and from the operator CLI. Both
paths share the same helper functions so audit invariants are
identical.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from omnigent.updater.protocol import (
    Authorization,
    RequestRecord,
    new_request_id,
    now_iso,
)
from omnigent.updater.store import DuplicateRequestError, UpdaterStore

_REQUEST_FIELDS: frozenset[str] = frozenset(
    {
        "target_sha",
        "expected_current_sha",
        "origin_session_id",
        "origin_conversation_id",
        "requested_by",
        "authorization",
        "notes",
    }
)


class RequestInterfaceError(RuntimeError):
    """Raised when a caller submits an invalid request payload."""


@dataclass(frozen=True)
class RequestOutcome:
    """The result of a single :func:`record_request` invocation."""

    request_id: str
    created_at: str
    request_path: str


def record_request(
    payload: dict[str, Any],
    *,
    store: UpdaterStore | None = None,
    state_root_override: str | None = None,
) -> RequestOutcome:
    """Validate ``payload`` and persist a new request file.

    The validation is strict: every field in the schema must be
    present in the right shape, and any extra field raises
    :class:`RequestInterfaceError`. The point of the strict schema
    is that a buggy or malicious caller cannot smuggle deployment
    privileges through the interface.

    On duplicate-request id collisions the function retries with a
    fresh id up to 5 times. If the new id also collides, the
    original :class:`DuplicateRequestError` is re-raised so the
    caller knows the durable store is in an unusual state.

    :param payload: Caller-supplied dict; see module docstring.
    :param store: Optional store override (tests use this).
    :param state_root_override: Optional state-root override.
    :returns: A :class:`RequestOutcome` with the durable id.
    """
    if state_root_override is not None:
        os.environ["OMNIGENT_UPDATER_STATE_ROOT"] = state_root_override
    store = store or UpdaterStore()
    parsed = _parse_payload(payload)
    last_exc: DuplicateRequestError | None = None
    for _ in range(5):
        rid = new_request_id()
        record = RequestRecord(
            request_id=rid,
            target_sha=parsed["target_sha"],
            expected_current_sha=parsed["expected_current_sha"],
            origin_session_id=parsed.get("origin_session_id"),
            origin_conversation_id=parsed.get("origin_conversation_id"),
            requested_by=parsed["requested_by"],
            created_at=now_iso(),
            authorization=parsed["authorization"],
            notes=parsed.get("notes"),
        )
        try:
            path = store.create_request(record)
        except DuplicateRequestError as exc:
            last_exc = exc
            continue
        return RequestOutcome(
            request_id=record.request_id,
            created_at=record.created_at,
            request_path=str(path),
        )
    raise last_exc or DuplicateRequestError("unable to allocate request id")


def _parse_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RequestInterfaceError("request payload must be a JSON object")

    unknown = set(payload) - _REQUEST_FIELDS
    if unknown:
        raise RequestInterfaceError(
            "request payload has unknown fields; the request interface only "
            "accepts the strict schema; got: " + ", ".join(sorted(unknown))
        )

    target_sha = payload.get("target_sha")
    if not (isinstance(target_sha, str) and len(target_sha) == 40):
        raise RequestInterfaceError("target_sha must be a 40-character string")
    if any(c not in "0123456789abcdef" for c in target_sha):
        raise RequestInterfaceError("target_sha must be exactly 40 lowercase hex characters")

    expected = payload.get("expected_current_sha")
    if not (isinstance(expected, str) and len(expected) == 40):
        raise RequestInterfaceError("expected_current_sha must be a 40-character string")
    if any(c not in "0123456789abcdef" for c in expected):
        raise RequestInterfaceError(
            "expected_current_sha must be exactly 40 lowercase hex characters"
        )

    origin_session_id = payload.get("origin_session_id")
    if origin_session_id is not None and not isinstance(origin_session_id, str):
        raise RequestInterfaceError("origin_session_id must be a string when set")

    origin_conversation_id = payload.get("origin_conversation_id")
    if origin_conversation_id is not None and not isinstance(origin_conversation_id, str):
        raise RequestInterfaceError("origin_conversation_id must be a string when set")

    requested_by = payload.get("requested_by")
    if not isinstance(requested_by, str) or not requested_by:
        raise RequestInterfaceError("requested_by is required")

    authorization_raw = payload.get("authorization")
    if not isinstance(authorization_raw, dict):
        raise RequestInterfaceError("authorization is required")
    try:
        authorization = Authorization.from_dict(authorization_raw)
    except (KeyError, ValueError, TypeError) as exc:
        raise RequestInterfaceError(f"authorization is invalid: {exc}") from exc

    notes = payload.get("notes")
    if notes is not None and not isinstance(notes, str):
        raise RequestInterfaceError("notes must be a string when set")

    return {
        "target_sha": target_sha,
        "expected_current_sha": expected,
        "origin_session_id": origin_session_id,
        "origin_conversation_id": origin_conversation_id,
        "requested_by": requested_by,
        "authorization": authorization,
        "notes": notes,
    }


# ----------------------------------------------------------------------
# The ``sys_update_request`` builtin adapter.
# ----------------------------------------------------------------------


def sys_update_request_payload(
    *,
    target_sha: str,
    expected_current_sha: str,
    origin_session_id: str | None = None,
    origin_conversation_id: str | None = None,
    requested_by: str = "verity",
    operator: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """Return the JSON payload accepted by the ``sys_update_request`` tool.

    This is the **only** shape the LLM is allowed to produce. The
    tool surface is intentionally narrower than the underlying
    request interface — no shell-like fields, no environment
    overrides, no service names, no paths.
    """
    payload: dict[str, Any] = {
        "target_sha": target_sha,
        "expected_current_sha": expected_current_sha,
        "requested_by": requested_by,
        "authorization": {"kind": "operator" if operator else "verity"},
    }
    if operator:
        payload["authorization"]["operator"] = operator
    if origin_session_id:
        payload["origin_session_id"] = origin_session_id
    if origin_conversation_id:
        payload["origin_conversation_id"] = origin_conversation_id
    if notes:
        payload["notes"] = notes
    return payload


def invoke_sys_update_request(
    *,
    target_sha: str,
    expected_current_sha: str,
    origin_session_id: str | None = None,
    origin_conversation_id: str | None = None,
    requested_by: str = "verity",
    operator: str | None = None,
    notes: str | None = None,
    store: UpdaterStore | None = None,
) -> dict[str, Any]:
    """Invoke ``sys_update_request`` as if it were called by the agent.

    Returns a dict shaped like the tool result the agent sees:

    * ``request_id`` — the durable id (return immediately so the
      caller can poll status);
    * ``created_at`` — ISO timestamp;
    * ``request_path`` — absolute path of the request file.
    """
    payload = sys_update_request_payload(
        target_sha=target_sha,
        expected_current_sha=expected_current_sha,
        origin_session_id=origin_session_id,
        origin_conversation_id=origin_conversation_id,
        requested_by=requested_by,
        operator=operator,
        notes=notes,
    )
    outcome = record_request(payload, store=store)
    return {
        "request_id": outcome.request_id,
        "created_at": outcome.created_at,
        "request_path": outcome.request_path,
    }


__all__ = [
    "RequestInterfaceError",
    "RequestOutcome",
    "invoke_sys_update_request",
    "record_request",
    "sys_update_request_payload",
]
