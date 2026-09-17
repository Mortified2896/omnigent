"""Caller-scoped response feedback on the first-class sessions surface."""

import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field, StrictInt

from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.task_experiment import Outcome, list_experiment_events, save_outcome
from omnigent.stores.conversation_store import ConversationStore, InvalidFeedbackTargetError
from omnigent.stores.permission_store import PermissionStore


class OutcomeInput(BaseModel):
    outcome: Outcome
    comment: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=8)


class FeedbackInput(BaseModel):
    """Omitting comment preserves it; explicit null clears it."""

    rating: Annotated[StrictInt, Field(ge=-1, le=1)]
    comment: str | None = Field(default=None, max_length=4000)


def register_feedback_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
) -> None:
    async def caller(request: Request, session_id: str, level: int) -> str:
        user_id = get_user_id(request, auth_provider)
        if auth_provider is not None and user_id is None:
            raise HTTPException(401, "Authentication required")
        access = await require_access_and_level(
            user_id,
            session_id,
            level,
            permission_store,
            conversation_store,
        )
        conv = access.conversation
        if conv is None:
            conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise session_not_found()
        return user_id or RESERVED_USER_LOCAL

    @router.get("/sessions/{session_id}/response-feedback")
    async def list_feedback(request: Request, session_id: str) -> list[dict]:
        user = await caller(request, session_id, LEVEL_READ)
        rows = await asyncio.to_thread(conversation_store.list_response_feedback, session_id, user)
        return [asdict(row) for row in rows]

    @router.put("/sessions/{session_id}/response-feedback/{response_id}")
    async def put_feedback(
        request: Request, session_id: str, response_id: str, body: FeedbackInput
    ) -> dict:
        user = await caller(request, session_id, LEVEL_EDIT)
        if body.rating not in (-1, 1):
            raise HTTPException(422, "Rating must be +1 or -1")
        try:
            row = await asyncio.to_thread(
                conversation_store.put_response_feedback,
                session_id,
                response_id,
                user,
                body.rating,
                comment=body.comment,
                update_comment="comment" in body.model_fields_set,
            )
        except InvalidFeedbackTargetError as exc:
            raise HTTPException(404, str(exc)) from exc
        return asdict(row)

    @router.delete("/sessions/{session_id}/response-feedback/{response_id}", status_code=204)
    async def delete_feedback(request: Request, session_id: str, response_id: str) -> Response:
        user = await caller(request, session_id, LEVEL_EDIT)
        try:
            await asyncio.to_thread(
                conversation_store.delete_response_feedback, session_id, response_id, user
            )
        except InvalidFeedbackTargetError as exc:
            raise HTTPException(404, str(exc)) from exc
        return Response(status_code=204)

    @router.get("/sessions/{session_id}/task-experiment")
    async def get_experiment(request: Request, session_id: str) -> list[dict]:
        user = await caller(request, session_id, LEVEL_READ)
        rows = await asyncio.to_thread(list_experiment_events, conversation_store, session_id)
        # Human revisions are caller-scoped. Model self-reviews are session-owned
        # evaluation records and are visible to every caller with READ access.
        return [
            row
            for row in rows
            if row["created_by"] == user or row.get("kind") == "model_review"
        ]

    @router.put("/sessions/{session_id}/task-outcomes/{response_id}")
    async def put_outcome(
        request: Request, session_id: str, response_id: str, body: OutcomeInput
    ) -> dict:
        user = await caller(request, session_id, LEVEL_EDIT)
        try:
            return await asyncio.to_thread(
                save_outcome,
                conversation_store,
                session_id,
                response_id,
                user,
                body.outcome,
                comment=body.comment,
                tags=body.tags,
            )
        except InvalidFeedbackTargetError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
