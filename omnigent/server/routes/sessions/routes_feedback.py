"""Caller-scoped response feedback on the first-class sessions surface."""

import asyncio
from dataclasses import asdict
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt

from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, RESERVED_USER_LOCAL, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.server.routes._errors import session_not_found
from omnigent.server.task_experiment import Outcome, list_experiment_events, save_outcome
from omnigent.server.task_scoring import (
    ExclusionReason,
    save_scoring_eligibility,
    scoring_policy,
    select_scored_outcomes,
)
from omnigent.stores.conversation_store import ConversationStore, InvalidFeedbackTargetError
from omnigent.stores.permission_store import PermissionStore


class OutcomeInput(BaseModel):
    outcome: Outcome
    comment: str | None = Field(default=None, max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=8)


class ScoringEligibilityInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    score_eligible: StrictBool
    exclusion_reason: ExclusionReason | None = None


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
        # Human revisions are caller-scoped and visible only to their author.
        return [row for row in rows if row["created_by"] == user]

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

    @router.get("/sessions/{session_id}/scoring-policy")
    async def get_scoring_policy(request: Request, session_id: str) -> dict:
        user = await caller(request, session_id, LEVEL_READ)
        try:
            return await asyncio.to_thread(scoring_policy, conversation_store, session_id, user)
        except ValueError as exc:
            raise session_not_found() from exc

    @router.get("/sessions/{session_id}/scored-outcomes")
    async def get_scored_outcomes(request: Request, session_id: str) -> list[dict]:
        user = await caller(request, session_id, LEVEL_READ)
        conv = await asyncio.to_thread(conversation_store.get_conversation, session_id)
        if conv is None:
            raise session_not_found()
        rows = await asyncio.to_thread(list_experiment_events, conversation_store, session_id)
        return select_scored_outcomes(
            rows, actor=user, conversation_id=session_id, labels=conv.labels or {}
        )

    @router.put("/sessions/{session_id}/scoring-eligibility/{response_id}")
    async def put_scoring_eligibility(
        request: Request, session_id: str, response_id: str, body: ScoringEligibilityInput
    ) -> dict:
        user = await caller(request, session_id, LEVEL_EDIT)
        try:
            return await asyncio.to_thread(
                save_scoring_eligibility,
                conversation_store,
                session_id,
                response_id,
                user,
                body.score_eligible,
                body.exclusion_reason,
            )
        except InvalidFeedbackTargetError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
