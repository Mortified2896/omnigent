"""Response-scoped video editing; no text, speech, or video inference calls."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
import subprocess
from typing import Any

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field, ValidationError

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ, AuthProvider
from omnigent.server.generated_audio_playback import _encode_lock
from omnigent.server.generated_response_audio import _assistant_text, _response_narration_for_id
from omnigent.server.generated_response_audio_timings import (
    timings_artifact_key,
    validate_timing_sidecar,
)
from omnigent.server.report_video import (
    MAX_VIDEO_BYTES,
    MAX_VIDEO_SECONDS,
    VideoManifest,
    build_video_scenes,
    download_commons_image,
    finish_report_video,
    read_video_json,
    search_commons,
    validate_manifest,
    video_assets_key,
    video_image_key,
    video_key,
)
from omnigent.server.routes._auth_helpers import get_user_id, require_access_and_level
from omnigent.stores import ConversationStore
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.generated_response_audio import SqlAlchemyGeneratedResponseAudioStore
from omnigent.stores.permission_store import PermissionStore


class ImageSearch(BaseModel):
    query: str = Field(min_length=2, max_length=180)


def _media(request: Request, content: bytes, mime: str) -> Response:
    etag = '"' + hashlib.sha256(content).hexdigest() + '"'
    headers = {
        "Cache-Control": "private, max-age=600",
        "ETag": etag,
        "Accept-Ranges": "bytes",
        "X-Content-Type-Options": "nosniff",
    }
    match = re.fullmatch(r"bytes=(\d{0,20})-(\d{0,20})", request.headers.get("range", ""))
    if match and request.headers.get("if-range", etag) == etag:
        first, last = match.groups()
        start = int(first) if first else max(0, len(content) - int(last or "0"))
        end = min(int(last), len(content) - 1) if first and last else len(content) - 1
        if start >= len(content) or end < start or not (first or last):
            return Response(
                status_code=416, headers={**headers, "Content-Range": f"bytes */{len(content)}"}
            )
        return Response(
            content[start : end + 1],
            status_code=206,
            media_type=mime,
            headers={**headers, "Content-Range": f"bytes {start}-{end}/{len(content)}"},
        )
    return Response(content, media_type=mime, headers=headers)


def register_report_video_routes(
    router: APIRouter,
    *,
    conversation_store: ConversationStore,
    artifact_store: ArtifactStore | None,
    audio_store: SqlAlchemyGeneratedResponseAudioStore | None,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
) -> None:
    async def authorize(request: Request, session_id: str, level: int = LEVEL_READ) -> None:
        await require_access_and_level(
            get_user_id(request, auth_provider),
            session_id,
            level,
            permission_store,
            conversation_store,
        )

    def ready(session_id: str, response_id: str) -> Any:
        if audio_store is None or artifact_store is None:
            raise OmnigentError(
                "Report video storage is unavailable", code=ErrorCode.RUNNER_UNAVAILABLE
            )
        entry = audio_store.get(session_id, response_id)
        if entry is None or entry.status != "ready" or not entry.artifact_key:
            raise OmnigentError(
                "This report needs a saved recording before creating video",
                code=ErrorCode.NOT_FOUND,
            )
        if not entry.duration_seconds or not 0 < entry.duration_seconds <= MAX_VIDEO_SECONDS:
            raise OmnigentError(
                "Video currently supports recordings up to 30 minutes",
                code=ErrorCode.INVALID_INPUT,
            )
        return entry

    @router.get("/sessions/{session_id}/generated-audio/{response_id}/video")
    async def project(request: Request, session_id: str, response_id: str) -> dict[str, Any]:
        await authorize(request, session_id)
        entry = await asyncio.to_thread(ready, session_id, response_id)
        assert artifact_store is not None
        try:
            audio = await asyncio.to_thread(artifact_store.get, entry.artifact_key)
        except KeyError as exc:
            raise OmnigentError(
                "Saved recording is unavailable", code=ErrorCode.NOT_FOUND
            ) from exc
        narration = await asyncio.to_thread(
            _response_narration_for_id, conversation_store, session_id, response_id
        )
        if not narration:
            raise OmnigentError(
                "The original report text is unavailable", code=ErrorCode.NOT_FOUND
            )
        page = await asyncio.to_thread(
            conversation_store.list_items, conversation_id=session_id, limit=1000, order="desc"
        )
        items = page.data
        markdown = "\n".join(
            _assistant_text(item)
            for item in reversed(items)
            if getattr(item, "response_id", None) == response_id
        )
        units = []
        try:
            payload = await asyncio.to_thread(
                artifact_store.get, timings_artifact_key(entry.artifact_key)
            )
            units = validate_timing_sidecar(
                payload,
                audio=audio,
                narration=narration,
                expected_duration_seconds=entry.duration_seconds,
            )["units"]
        except (KeyError, ValueError):
            pass
        saved = await asyncio.to_thread(
            read_video_json, artifact_store, video_key(entry.artifact_key) + ".json"
        )
        digest = hashlib.sha256(audio).hexdigest()
        if saved.get("audio_sha256") != digest or not artifact_store.exists(
            video_key(entry.artifact_key)
        ):
            saved = {}
        return {
            "schema_version": 1,
            "audio_sha256": digest,
            "duration": entry.duration_seconds,
            "scenes": saved.get("scenes")
            or build_video_scenes(markdown, narration, entry.duration_seconds, units),
            "timing": "native" if units else "estimated",
            "units": units,
            "images": await asyncio.to_thread(
                read_video_json, artifact_store, video_assets_key(entry.artifact_key)
            ),
            "saved": saved or None,
            "presenter": "finance-vector",
            "ffmpeg_available": bool(shutil.which("ffmpeg") and shutil.which("ffprobe")),
        }

    @router.post("/sessions/{session_id}/generated-audio/{response_id}/video/images")
    async def images(
        request: Request, session_id: str, response_id: str, body: ImageSearch
    ) -> dict[str, Any]:
        await authorize(request, session_id, LEVEL_EDIT)
        entry = await asyncio.to_thread(ready, session_id, response_id)
        assert artifact_store is not None
        results = []
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                candidates = await search_commons(client, body.query.strip())
                for candidate in candidates:
                    # Network fetches never forward user cookies or credentials.
                    payload, mime = await download_commons_image(client, candidate["url"])

                    def save_image(
                        candidate: dict[str, str] = candidate,
                        payload: bytes = payload,
                        mime: str = mime,
                    ) -> dict[str, str]:
                        with _encode_lock:
                            ready(session_id, response_id)
                            assets = read_video_json(
                                artifact_store, video_assets_key(entry.artifact_key)
                            )
                            if candidate["id"] not in assets and len(assets) >= 24:
                                raise ValueError("This recording's image cache is full")
                            record = {
                                key: value for key, value in candidate.items() if key != "url"
                            }
                            record["mime"] = mime
                            artifact_store.put(
                                video_image_key(entry.artifact_key, candidate["id"]), payload
                            )
                            assets[candidate["id"]] = record
                            artifact_store.put(
                                video_assets_key(entry.artifact_key), json.dumps(assets).encode()
                            )
                            return record

                    results.append(await asyncio.to_thread(save_image))
        except (httpx.HTTPError, ValueError) as exc:
            raise OmnigentError(
                "Image search is unavailable. You can still create the video with graphic cards.",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        return {"images": results}

    @router.get("/sessions/{session_id}/generated-audio/{response_id}/video/images/{image_id}")
    async def image(
        request: Request, session_id: str, response_id: str, image_id: str
    ) -> Response:
        await authorize(request, session_id)
        entry = await asyncio.to_thread(ready, session_id, response_id)
        assert artifact_store is not None
        assets = await asyncio.to_thread(
            read_video_json, artifact_store, video_assets_key(entry.artifact_key)
        )
        record = assets.get(image_id)
        if not record or not re.fullmatch(r"[a-f0-9]{64}", image_id):
            raise OmnigentError("Video image not found", code=ErrorCode.NOT_FOUND)
        try:
            payload = await asyncio.to_thread(
                artifact_store.get, video_image_key(entry.artifact_key, image_id)
            )
        except KeyError as exc:
            raise OmnigentError("Video image not found", code=ErrorCode.NOT_FOUND) from exc
        return _media(request, payload, record["mime"])

    @router.post("/sessions/{session_id}/generated-audio/{response_id}/video")
    async def finish(request: Request, session_id: str, response_id: str) -> dict[str, Any]:
        await authorize(request, session_id, LEVEL_EDIT)
        entry = await asyncio.to_thread(ready, session_id, response_id)
        assert artifact_store is not None
        data = bytearray()
        async for chunk in request.stream():
            data.extend(chunk)
            if len(data) > MAX_VIDEO_BYTES + 100_000:
                raise OmnigentError("Video upload exceeds 128 MB", code=ErrorCode.INVALID_INPUT)
        raw_manifest, separator, visuals = bytes(data).partition(b"\n")
        if not separator or len(raw_manifest) > 100_000:
            raise OmnigentError("Missing video manifest", code=ErrorCode.INVALID_INPUT)
        try:
            manifest = VideoManifest.model_validate_json(raw_manifest)
            audio = await asyncio.to_thread(artifact_store.get, entry.artifact_key)
            validate_manifest(manifest, audio, entry.duration_seconds)
            assets = await asyncio.to_thread(
                read_video_json, artifact_store, video_assets_key(entry.artifact_key)
            )
            if any(scene.image_id and scene.image_id not in assets for scene in manifest.scenes):
                raise ValueError("A selected image is unavailable")
            output = await asyncio.to_thread(
                finish_report_video, visuals, audio, manifest.duration
            )
        except (ValueError, KeyError, ValidationError, subprocess.SubprocessError) as exc:
            raise OmnigentError(
                "Could not finish the video. Check the recording and local FFmpeg, then retry.",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
        record = {
            **manifest.model_dump(),
            "video_sha256": hashlib.sha256(output).hexdigest(),
            "byte_size": len(output),
            "render_schema": 1,
        }

        def publish() -> None:
            with _encode_lock:
                ready(session_id, response_id)
                artifact_store.put(video_key(entry.artifact_key), output)
                artifact_store.put(
                    video_key(entry.artifact_key) + ".json", json.dumps(record).encode()
                )

        await asyncio.to_thread(publish)
        return record

    @router.get("/sessions/{session_id}/generated-audio/{response_id}/video/content")
    async def content(request: Request, session_id: str, response_id: str) -> Response:
        await authorize(request, session_id)
        entry = await asyncio.to_thread(ready, session_id, response_id)
        assert artifact_store is not None
        try:
            payload = await asyncio.to_thread(artifact_store.get, video_key(entry.artifact_key))
        except KeyError as exc:
            raise OmnigentError("Report video not found", code=ErrorCode.NOT_FOUND) from exc
        return _media(request, payload, "video/mp4")
