"""Generated response audio read API tests."""

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.routes.sessions.routes_generated_audio import (
    register_generated_audio_routes,
)
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.generated_response_audio import (
    SqlAlchemyGeneratedResponseAudioStore,
)


def test_list_and_fetch_audio_by_exact_response(db_uri: str, tmp_path) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    conversation = conversations.create_conversation()
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    audio_store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    pending = audio_store.create_pending(conversation.id, "answer-1", "daily-brief")
    assert pending.status == "pending"
    assert audio_store.claim_pending(conversation.id, "answer-1") is True
    artifact_key = "generated-response-audio/example.wav"
    wav = b"RIFF\x00\x00\x00\x00WAVE"
    artifact_store.put(artifact_key, wav)
    audio_store.mark_ready(
        conversation.id,
        "answer-1",
        artifact_key=artifact_key,
        duration_seconds=0.1,
        sample_rate=24_000,
    )

    router = APIRouter()
    register_generated_audio_routes(
        router,
        conversation_store=conversations,
        artifact_store=artifact_store,
        audio_store=audio_store,
        auth_provider=None,
        permission_store=None,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        listing = client.get(f"/v1/sessions/{conversation.id}/generated-audio")
        assert listing.status_code == 200
        assert listing.json()["data"] == [
            {
                "response_id": "answer-1",
                "status": "ready",
                "duration_seconds": 0.1,
                "sample_rate": 24_000,
                "error_code": None,
                "updated_at": listing.json()["data"][0]["updated_at"],
            }
        ]
        content = client.get(f"/v1/sessions/{conversation.id}/generated-audio/answer-1/content")
        assert content.status_code == 200
        assert content.headers["content-type"] == "audio/wav"
        assert content.content == wav
        assert (
            client.get(f"/v1/sessions/{conversation.id}/generated-audio/other/content").status_code
            == 404
        )
