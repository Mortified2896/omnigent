"""Video routes enforce conversation read/edit access and scoped retention."""

import hashlib
import json
from types import SimpleNamespace

from fastapi import APIRouter, FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from omnigent.errors import OmnigentError
from omnigent.server.report_video import video_key
from omnigent.server.routes.sessions import routes_report_video as routes
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from omnigent.stores.generated_response_audio import SqlAlchemyGeneratedResponseAudioStore
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore


def test_project_and_publish_require_scoped_access_and_unchanged_audio(
    db_uri, tmp_path, monkeypatch
):
    conversations = SqlAlchemyConversationStore(db_uri)
    conversation = conversations.create_conversation()
    permissions = SqlAlchemyPermissionStore(db_uri)
    permissions.grant("reader", conversation.id, 1)
    permissions.grant("editor", conversation.id, 2)
    artifacts = LocalArtifactStore(str(tmp_path))
    audios = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    key = "generated-response-audio/example.wav"
    original = b"exact original waveform"
    artifacts.put(key, original)
    audios.create_pending(conversation.id, "report", "voice")
    audios.claim_pending(conversation.id, "report")
    audios.mark_ready(
        conversation.id, "report", artifact_key=key, duration_seconds=10, sample_rate=24000
    )

    class Caller:
        def get_user_id(self, request):
            return request.headers.get("x-user")

    class Reports:
        def get_conversation(self, cid):
            return conversations.get_conversation(cid)

        def list_items(self, **kwargs):
            return SimpleNamespace(
                data=[
                    SimpleNamespace(
                        response_id="report",
                        data=SimpleNamespace(
                            role="assistant",
                            is_meta=False,
                            content=[
                                {
                                    "type": "output_text",
                                    "text": "# Daily Report\nOriginal report news.",
                                }
                            ],
                        ),
                    ),
                    SimpleNamespace(
                        type="function_call",
                        response_id="report",
                        data=SimpleNamespace(name="web_search"),
                    ),
                    SimpleNamespace(
                        type="message",
                        response_id="report",
                        data=SimpleNamespace(
                            role="assistant",
                            content=[
                                {
                                    "type": "output_text",
                                    "text": "# Checking the Mac\nProgress only.",
                                }
                            ],
                        ),
                    ),
                ]
            )

    monkeypatch.setattr(
        routes, "_response_narration_for_id", lambda *args: "Daily Report Original report news."
    )
    renders = []

    def render(visuals, audio, duration):
        renders.append((visuals, audio, duration))
        return b"local finished video"

    monkeypatch.setattr(routes, "finish_report_video", render)
    image_id = "c" * 64

    async def search_images(client, query):
        return [
            {
                "id": image_id,
                "title": "Official portrait",
                "url": "https://upload.wikimedia.org/portrait.jpg",
                "source": "https://commons.wikimedia.org/wiki/File:Portrait",
                "credit": "Photographer",
                "license": "Public domain",
            }
        ]

    async def download(client, url):
        return b"cached JPEG", "image/jpeg"

    monkeypatch.setattr(routes, "search_commons", search_images)
    monkeypatch.setattr(routes, "download_commons_image", download)
    router = APIRouter()
    routes.register_report_video_routes(
        router,
        conversation_store=Reports(),
        artifact_store=artifacts,
        audio_store=audios,
        auth_provider=Caller(),
        permission_store=permissions,
    )
    app = FastAPI()

    @app.exception_handler(OmnigentError)
    async def handle(request: Request, error: OmnigentError):
        return JSONResponse(status_code=error.http_status, content={"detail": error.message})

    app.include_router(router, prefix="/v1")
    base = f"/v1/sessions/{conversation.id}/generated-audio/report/video"
    reader, editor = {"x-user": "reader"}, {"x-user": "editor"}
    with TestClient(app) as client:
        for path in (base, base + "/content", base + "/images/" + "a" * 64):
            assert client.get(path).status_code == 401
            assert client.get(path, headers={"x-user": "stranger"}).status_code == 404
        assert client.post(base, content=b"untrusted", headers=reader).status_code == 403
        assert (
            client.post(base + "/images", json={"query": "Trump"}, headers=reader).status_code
            == 403
        )
        project = client.get(base, headers=reader).json()
        assert project["timing"] == "estimated" and project["saved"] is None
        assert "Checking the Mac" not in json.dumps(project["scenes"])
        assert "Progress only" not in json.dumps(project["scenes"])
        assert project["audio_sha256"] == hashlib.sha256(original).hexdigest()
        manifest = {
            key: project[key] for key in ("schema_version", "audio_sha256", "duration", "scenes")
        }

        def body():
            return json.dumps(manifest).encode() + b"\nvisuals"

        manifest["audio_sha256"] = "0" * 64
        assert client.post(base, content=body(), headers=editor).status_code == 400
        assert renders == []
        manifest["audio_sha256"] = project["audio_sha256"]
        assert client.post(base, content=body(), headers=editor).status_code == 200
        assert renders == [(b"visuals", original, 10)]
        assert artifacts.get(key) == original
        images = client.post(base + "/images", json={"query": "Donald Trump"}, headers=editor)
        assert images.status_code == 200 and images.json()["images"][0]["id"] == image_id
        assert "url" not in images.json()["images"][0]
        assert client.get(base + "/images/" + image_id, headers=reader).content == b"cached JPEG"
        assert client.get(base + "/images/" + "d" * 64, headers=reader).status_code == 404
        monkeypatch.setattr(routes, "MAX_VIDEO_BYTES", 1)
        assert client.post(base, content=b"x" * 100_002, headers=editor).status_code == 400
        assert len(renders) == 1
        saved = client.get(base, headers=reader).json()["saved"]
        assert saved["video_sha256"] == hashlib.sha256(b"local finished video").hexdigest()
        part = client.get(base + "/content", headers={**reader, "range": "bytes=0-4"})
        assert part.status_code == 206 and part.content == b"local"
        assert (
            client.get(base + "/content", headers={**reader, "range": "bytes=999-"}).status_code
            == 416
        )
        assert audios.claim_expired(audios.get(conversation.id, "report"))
        assert client.get(base + "/content", headers=reader).status_code == 404
        assert client.post(base, content=body(), headers=editor).status_code == 404
        assert artifacts.exists(video_key(key))
