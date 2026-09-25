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
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


class Caller:
    def get_user_id(self, request):
        return request.headers.get("x-test-user")


def test_list_and_fetch_audio_by_exact_response(db_uri: str, tmp_path) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    conversation = conversations.create_conversation()
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("alice", conversation.id, 1)
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
        auth_provider=Caller(),
        permission_store=permissions,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def _handle_error(request: Request, exc: OmnigentError):
        return JSONResponse(status_code=exc.http_status, content={"detail": exc.message})

    app.include_router(router, prefix="/v1")
    with TestClient(app) as client:
        url = f"/v1/sessions/{conversation.id}/generated-audio"
        assert client.get(url).status_code == 401
        assert client.get(url, headers={"x-test-user": "stranger"}).status_code == 404
        headers = {"x-test-user": "alice"}
        listing = client.get(url, headers=headers)
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
        content = client.get(
            f"{url}/answer-1/content",
            headers=headers,
        )
        assert content.status_code == 200
        assert content.headers["content-type"] == "audio/wav"
        assert content.content == wav
        assert client.get(f"{url}/other/content", headers=headers).status_code == 404

        content_url = f"{url}/answer-1/content"
        for byte_range, start, end in [
            ("bytes=0-1", 0, 1),
            ("bytes=4-", 4, 11),
            ("bytes=-4", 8, 11),
            ("bytes=0-999", 0, 11),
        ]:
            partial = client.get(content_url, headers={**headers, "Range": byte_range})
            assert partial.status_code == 206
            assert partial.content == wav[start : end + 1]
            assert partial.headers["content-range"] == f"bytes {start}-{end}/{len(wav)}"
            assert partial.headers["accept-ranges"] == "bytes"
            assert int(partial.headers["content-length"]) == end - start + 1
        for byte_range in ["bytes=99-", "bytes=6-2", "bytes=-0", "bytes=-"]:
            invalid = client.get(content_url, headers={**headers, "Range": byte_range})
            assert invalid.status_code == 416
            assert invalid.headers["content-range"] == f"bytes */{len(wav)}"
        assert client.get(content_url, headers={"Range": "bytes=0-1"}).status_code == 401
        assert (
            client.get(
                content_url, headers={"x-test-user": "stranger", "Range": "bytes=0-1"}
            ).status_code
            == 404
        )
        assert (
            client.get(
                content_url, headers={**headers, "Range": "bytes=0-1", "If-Range": "stale"}
            ).status_code
            == 200
        )
