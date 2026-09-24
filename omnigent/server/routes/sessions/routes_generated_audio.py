"""Read-only routes for generated audio attached to assistant responses."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, Request
from fastapi.responses import Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import (
    get_user_id,
    require_access_and_level,
)
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.generated_response_audio import (
    SqlAlchemyGeneratedResponseAudioStore,
)
from omnigent.stores.permission_store import PermissionStore


def register_generated_audio_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore | None,
    audio_store: SqlAlchemyGeneratedResponseAudioStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    """Register response-scoped generated-audio metadata and content routes."""

    def _require_audio_store() -> SqlAlchemyGeneratedResponseAudioStore:
        if audio_store is None:
            raise OmnigentError(
                "Generated response audio is not configured",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        if artifact_store is None:
            raise OmnigentError(
                "Generated response audio storage is not configured",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        return audio_store

    async def _authorize(request: Request, session_id: str) -> None:
        access = await require_access_and_level(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        if access.conversation is None:
            conversation = await asyncio.to_thread(
                conversation_store.get_conversation,
                session_id,
            )
            if conversation is None:
                raise OmnigentError(
                    "Session not found",
                    code=ErrorCode.NOT_FOUND,
                )

    @router.get("/sessions/{session_id}/generated-audio")
    async def list_generated_audio(request: Request, session_id: str) -> dict[str, object]:
        await _authorize(request, session_id)
        store = _require_audio_store()
        entries = await asyncio.to_thread(store.list_for_conversation, session_id)
        return {
            "data": [
                {
                    "response_id": row.response_id,
                    "status": row.status,
                    "duration_seconds": row.duration_seconds,
                    "sample_rate": row.sample_rate,
                    "error_code": row.error_code,
                    "updated_at": row.updated_at,
                }
                for row in entries
            ]
        }

    @router.get("/sessions/{session_id}/generated-audio/{response_id}/content")
    async def get_generated_audio_content(
        request: Request,
        session_id: str,
        response_id: str,
    ) -> Response:
        await _authorize(request, session_id)
        store = _require_audio_store()
        entry = await asyncio.to_thread(store.get, session_id, response_id)
        if entry is None or entry.status != "ready" or not entry.artifact_key:
            raise OmnigentError(
                "Generated response audio not found",
                code=ErrorCode.NOT_FOUND,
            )
        try:
            assert artifact_store is not None
            content = await asyncio.to_thread(artifact_store.get, entry.artifact_key)
        except KeyError as exc:
            raise OmnigentError(
                "Generated response audio artifact not found",
                code=ErrorCode.NOT_FOUND,
            ) from exc
        return Response(
            content=content,
            media_type="audio/wav",
            headers={
                "Content-Disposition": 'inline; filename="response-audio.wav"',
                "Cache-Control": "private, max-age=31536000, immutable",
                "X-Content-Type-Options": "nosniff",
            },
        )
