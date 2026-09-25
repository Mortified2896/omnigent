"""Generated response audio read API tests."""

import hashlib
import io
import json
import wave
from types import SimpleNamespace

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
        assert client.get(f"{url}/answer-1/timings", headers=headers).status_code == 404

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


def test_timing_route_authorizes_and_rejects_missing_malformed_or_stale_sidecars(
    db_uri: str,
    tmp_path,
) -> None:
    conversations = SqlAlchemyConversationStore(db_uri)
    conversation = conversations.create_conversation()

    class ResponseStore:
        def get_conversation(self, conversation_id):
            return conversations.get_conversation(conversation_id)

        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        response_id="answer-1",
                        data=SimpleNamespace(
                            role="assistant",
                            is_meta=False,
                            content=[{"type": "output_text", "text": "Hello world."}],
                        ),
                    )
                ]
            )

    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("alice", conversation.id, 1)
    artifact_store = LocalArtifactStore(str(tmp_path / "artifacts"))
    audio_store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    audio_store.create_pending(conversation.id, "answer-1", "kokoro-heart")
    assert audio_store.claim_pending(conversation.id, "answer-1") is True
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        target.writeframes(b"\x00\x00" * 2400)
    wav = buffer.getvalue()
    artifact_key = "generated-response-audio/example.wav"
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
        conversation_store=ResponseStore(),
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
    base = f"/v1/sessions/{conversation.id}/generated-audio/answer-1"
    with TestClient(app) as client:
        assert client.get(f"{base}/timings").status_code == 401
        assert client.get(f"{base}/timings", headers={"x-test-user": "stranger"}).status_code == 404
        headers = {"x-test-user": "alice"}
        assert client.get(f"{base}/timings", headers=headers).status_code == 404
        assert client.get(f"{base}/content", headers=headers).content == wav

        narration = "Hello world."
        sidecar = {
            "schema_version": 1,
            "engine": "kokoro",
            "audio_sha256": hashlib.sha256(wav).hexdigest(),
            "narration_sha256": hashlib.sha256(narration.encode()).hexdigest(),
            "duration_seconds": 0.1,
            "units": [
                {
                    "text": "Hello",
                    "narration_start": 0,
                    "narration_end": 5,
                    "start_seconds": 0.0,
                    "end_seconds": 0.05,
                },
                {
                    "text": "world",
                    "narration_start": 6,
                    "narration_end": 11,
                    "start_seconds": 0.05,
                    "end_seconds": 0.1,
                },
            ],
        }
        sidecar_key = f"{artifact_key}.timings.json"
        artifact_store.put(sidecar_key, json.dumps(sidecar).encode())
        response = client.get(f"{base}/timings", headers=headers)
        assert response.status_code == 200
        assert response.json()["engine"] == "kokoro"
        assert [unit["text"] for unit in response.json()["units"]] == ["Hello", "world"]

        sidecar["audio_sha256"] = "0" * 64
        artifact_store.put(sidecar_key, json.dumps(sidecar).encode())
        assert client.get(f"{base}/timings", headers=headers).status_code == 404
        assert client.get(f"{base}/content", headers=headers).content == wav

        artifact_store.put(sidecar_key, b"{malformed")
        assert client.get(f"{base}/timings", headers=headers).status_code == 404
        assert client.get(f"{base}/content", headers=headers).content == wav
