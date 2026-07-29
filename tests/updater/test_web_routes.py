"""Tests for the web-service updater endpoints (issue #38 §4, §8)."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest

from omnigent.server.routes.updater import (
    CancelRequest,
    DeliverRequest,
    DeliverResponse,
    _already_delivered,
    _check_token,
    _mark_delivered,
    create_updater_router,
)
from omnigent.stores.comment_store import CommentStore


@pytest.fixture
def tmp_state_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "updates"
    root.mkdir()
    monkeypatch.setenv("OMNIGENT_UPDATER_STATE_ROOT", str(root))
    return root


@pytest.fixture
def comment_store(tmp_path: Path) -> Iterator[CommentStore]:
    db_path = tmp_path / "comments.db"
    store = CommentStore(storage_location=f"sqlite:///{db_path}")
    yield store


def test_check_token_accepts_matching_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_UPDATER_TOKEN", "secret-123")
    from fastapi import Request

    req = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/api/updater/result-deliver",
            "headers": [(b"x-omnigent-updater-token", b"secret-123")],
            "raw_path": b"/api/updater/result-deliver",
        }
    )
    assert _check_token(req) is True


def test_check_token_rejects_wrong_secret(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_UPDATER_TOKEN", "secret-123")
    from fastapi import Request

    req = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/api/updater/result-deliver",
            "headers": [(b"x-omnigent-updater-token", b"wrong")],
            "raw_path": b"/api/updater/result-deliver",
        }
    )
    assert _check_token(req) is False


def test_check_token_fails_closed_when_unset() -> None:
    """A missing token env var refuses every header."""
    os.environ.pop("OMNIGENT_UPDATER_TOKEN", None)
    from fastapi import Request

    req = Request(
        scope={
            "type": "http",
            "method": "POST",
            "path": "/api/updater/result-deliver",
            "headers": [],
            "raw_path": b"/api/updater/result-deliver",
        }
    )
    assert _check_token(req) is False


def test_idempotency_marker_round_trip(tmp_state_root: Path) -> None:
    """``_mark_delivered`` followed by ``_already_delivered`` round-trips."""
    rid = "AAAAAAAAAAAAAAAAAAAAAAAAAA"
    _mark_delivered(tmp_state_root, rid, "comment-id-1")
    assert _already_delivered(tmp_state_root, rid) == "comment-id-1"


def test_idempotency_marker_missing_returns_none(tmp_state_root: Path) -> None:
    assert _already_delivered(tmp_state_root, "BBBBBBBBBBBBBBBBBBBBBBBBBB") is None


def test_create_updater_router_includes_four_endpoints() -> None:
    """The router exposes exactly the four documented endpoints."""
    router = create_updater_router(
        auth_provider=None,
        conversation_store=None,
        comment_store=None,
    )
    paths = sorted(route.path for route in router.routes)
    assert paths == [
        "/api/updater/drain-status",
        "/api/updater/request-cancel",
        "/api/updater/requests/{request_id}",
        "/api/updater/result-deliver",
    ]


def test_deliver_request_rejects_unknown_status() -> None:
    """The Pydantic model refuses a non-terminal ``final_status`` at the API boundary."""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        DeliverRequest(
            request_id="A" * 26,
            final_status="not_a_terminal_state",
            target_sha="0" * 40,
            previous_sha="0" * 40,
            deployed_sha="0" * 40,
        )


def test_cancel_request_rejects_extra_fields() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        CancelRequest(session_id="s1", shell_command="rm -rf /")


def test_deliver_response_serializes_correctly() -> None:
    response = DeliverResponse(delivered=True, comment_id="abc", message="ok")
    assert response.delivered is True
    assert response.comment_id == "abc"
    assert response.message == "ok"
