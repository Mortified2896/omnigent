"""Low-cost report video plans, Commons images, and original-audio finishing."""

from __future__ import annotations

import hashlib
import html
import json
import math
import re
import shutil
import subprocess
import tempfile
from itertools import pairwise
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field

from omnigent.server.generated_response_audio import clean_narration
from omnigent.stores.artifact_store import ArtifactStore

MAX_VIDEO_BYTES = 128 * 1024 * 1024
MAX_IMAGE_BYTES = 4 * 1024 * 1024
MAX_VIDEO_SECONDS = 1800
_ID = re.compile(r"[a-f0-9]{64}")
_USER_AGENT = "OmnigentReportVideo/1.0 (https://github.com/Mortified2896/omnigent)"


class VideoScene(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start: float = Field(ge=0, allow_inf_nan=False)
    title: str = Field(min_length=1, max_length=160)
    summary: str = Field(max_length=600)
    image_query: str = Field(max_length=180)
    image_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


class VideoManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: int = Field(default=1, ge=1, le=1)
    audio_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    duration: float = Field(gt=0, le=MAX_VIDEO_SECONDS, allow_inf_nan=False)
    scenes: list[VideoScene] = Field(min_length=1, max_length=32)


def video_key(audio_key: str) -> str:
    return audio_key + ".video.mp4"


def video_assets_key(audio_key: str) -> str:
    return audio_key + ".video-assets.json"


def video_image_key(audio_key: str, image_id: str) -> str:
    if not _ID.fullmatch(image_id):
        raise ValueError("Invalid video image ID")
    return audio_key + ".video-image-" + image_id


def read_video_json(store: ArtifactStore, key: str) -> dict[str, Any]:
    try:
        payload = store.get(key)
        if len(payload) > 100_000:
            return {}
        result = json.loads(payload)
        return result if isinstance(result, dict) else {}
    except (KeyError, ValueError):
        return {}


def delete_video_artifacts(store: ArtifactStore, audio_key: str) -> None:
    assets = read_video_json(store, video_assets_key(audio_key))
    for image_id in list(assets)[:24]:
        if _ID.fullmatch(image_id):
            store.delete(video_image_key(audio_key, image_id))
    store.delete(video_assets_key(audio_key))
    store.delete(video_key(audio_key) + ".json")
    store.delete(video_key(audio_key))


def build_video_scenes(
    markdown: str, narration: str, duration: float, units: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Use exact narration anchors when available; old recordings are estimated."""
    markdown = re.sub(r"```[^\n]*\n.*?```", "", markdown, flags=re.DOTALL)
    scenes: list[dict[str, Any]] = []
    offset = 0
    section = "Daily Report"
    for raw in markdown.splitlines():
        spoken = clean_narration(raw)
        if not spoken or len(spoken) < 12:
            continue
        found = narration.find(spoken, offset)
        if found < 0:
            continue
        offset = found + len(spoken)
        heading = bool(re.match(r"^\s*(?:#{1,6}\s|(?:\*\*)?\d+[ —–])", raw))
        label = re.match(r"^\s*[-*]\s+\*\*([^*]{3,160})\*\*", raw)
        if heading:
            section = re.sub(r"[*#]", "", raw).strip()[:160]
        title = label.group(1).rstrip(":") if label else section
        start = duration * found / max(1, len(narration))
        if units:
            next_unit = next((unit for unit in units if unit["narration_start"] >= found), None)
            if next_unit and next_unit["narration_start"] < found + len(spoken):
                start = float(next_unit["start_seconds"])
        start = min(start, max(0, duration - 0.1))
        if scenes and (start <= scenes[-1]["start"] + 3 or heading):
            continue
        query = next(
            (
                term
                for term in (
                    "Donald Trump",
                    "Elon Musk",
                    "Omnigent",
                    "OmniRoute",
                    "OpenAI",
                    "Gemini",
                )
                if term.casefold() in (title + " " + spoken).casefold()
            ),
            title,
        )
        scenes.append(
            {
                "start": start,
                "title": title[:160],
                "summary": spoken[:600],
                "image_query": query[:180],
                "image_id": None,
            }
        )
    if not scenes:
        scenes = [
            {
                "start": 0,
                "title": "Daily Report",
                "summary": narration[:600],
                "image_query": "",
                "image_id": None,
            }
        ]
    scenes[0]["start"] = 0
    if len(scenes) > 32:
        scenes = [scenes[index * (len(scenes) - 1) // 31] for index in range(32)]
    return scenes


def validate_manifest(manifest: VideoManifest, audio: bytes, duration: float) -> None:
    if manifest.audio_sha256 != hashlib.sha256(audio).hexdigest():
        raise ValueError("The saved narration changed; reopen the video editor")
    if abs(manifest.duration - duration) > 0.05:
        raise ValueError("The video must use the complete original narration")
    starts = [scene.start for scene in manifest.scenes]
    if starts[0] != 0 or any(a >= b for a, b in pairwise(starts)) or starts[-1] >= duration:
        raise ValueError("Scene times must be ordered within the recording")


def _plain(value: object, maximum: int = 240) -> str:
    return html.unescape(re.sub(r"<[^>]*>", "", str(value))).strip()[:maximum]


def _commons_url(url: str, host: str) -> bool:
    try:
        parsed = urlsplit(url)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and parsed.hostname == host
        and not parsed.username
        and not parsed.password
        and port in (None, 443)
    )


async def search_commons(client: httpx.AsyncClient, query: str) -> list[dict[str, str]]:
    response = await client.get(
        "https://commons.wikimedia.org/w/api.php",
        params={
            "action": "query",
            "generator": "search",
            "gsrsearch": query,
            "gsrnamespace": "6",
            "gsrlimit": "4",
            "prop": "imageinfo",
            "iiprop": "url|extmetadata|mime|thumbmime",
            "iiurlwidth": "960",
            "format": "json",
        },
        headers={"User-Agent": _USER_AGENT},
        follow_redirects=False,
    )
    response.raise_for_status()
    if len(response.content) > 1_000_000:
        raise ValueError("Image search response is too large")
    results = []
    for page in response.json().get("query", {}).get("pages", {}).values():
        info = (page.get("imageinfo") or [{}])[0]
        meta = info.get("extmetadata", {})
        license_name = _plain(meta.get("LicenseShortName", {}).get("value", ""))
        thumbnail = info.get("thumburl", "")
        source = info.get("descriptionurl", "")
        if (
            not re.match(r"^(Public domain|CC0|CC BY(?:-SA)?\s)", license_name)
            or not any(
                _commons_url(thumbnail, host)
                for host in ("upload.wikimedia.org", "thumb.wikimedia.org")
            )
            or not _commons_url(source, "commons.wikimedia.org")
            or info.get("thumbmime", info.get("mime"))
            not in {"image/jpeg", "image/png", "image/webp"}
        ):
            continue
        results.append(
            {
                "id": hashlib.sha256(thumbnail.encode()).hexdigest(),
                "title": _plain(page.get("title", "")),
                "url": thumbnail,
                "source": source,
                "credit": _plain(meta.get("Artist", {}).get("value", "Wikimedia Commons")),
                "license": license_name,
            }
        )
    return results


async def download_commons_image(client: httpx.AsyncClient, url: str) -> tuple[bytes, str]:
    if not any(
        _commons_url(url, host) for host in ("upload.wikimedia.org", "thumb.wikimedia.org")
    ):
        raise ValueError("Only verified Commons thumbnails can be fetched")
    async with client.stream(
        "GET", url, headers={"User-Agent": _USER_AGENT}, follow_redirects=False
    ) as response:
        response.raise_for_status()
        mime = response.headers.get("content-type", "").split(";")[0]
        if mime not in {"image/jpeg", "image/png", "image/webp"}:
            raise ValueError("Unsupported image type")
        data = bytearray()
        async for chunk in response.aiter_bytes():
            data.extend(chunk)
            if len(data) > MAX_IMAGE_BYTES:
                raise ValueError("Image exceeds 4 MB")
    payload = bytes(data)
    valid = (
        (mime == "image/jpeg" and payload.startswith(b"\xff\xd8\xff"))
        or (mime == "image/png" and payload.startswith(b"\x89PNG\r\n\x1a\n"))
        or (mime == "image/webp" and payload[:4] == b"RIFF" and payload[8:12] == b"WEBP")
    )
    if not valid:
        raise ValueError("Image content does not match its type")
    return payload, mime


def finish_report_video(visuals: bytes, audio: bytes, duration: float) -> bytes:
    """Copy browser-rendered H.264, mux the original WAV with AAC skip metadata."""
    ffmpeg, probe = shutil.which("ffmpeg"), shutil.which("ffprobe")
    if not ffmpeg or not probe:
        raise ValueError("Install local FFmpeg to finish the video")
    if (
        not 0 < duration <= MAX_VIDEO_SECONDS
        or len(visuals) > MAX_VIDEO_BYTES
        or visuals[4:8] != b"ftyp"
    ):
        raise ValueError("Invalid video upload")
    with tempfile.TemporaryDirectory(prefix="omnigent-video-") as root:
        path = Path(root)
        (path / "visuals.mp4").write_bytes(visuals)
        (path / "original.wav").write_bytes(audio)
        info = json.loads(
            subprocess.check_output(
                [
                    probe,
                    "-v",
                    "error",
                    "-protocol_whitelist",
                    "file,pipe",
                    "-enable_drefs",
                    "0",
                    "-f",
                    "mov",
                    "-show_streams",
                    "-of",
                    "json",
                    str(path / "visuals.mp4"),
                ],
                timeout=15,
                stderr=subprocess.DEVNULL,
            )
        )
        streams = info.get("streams", [])
        if len(streams) != 1:
            raise ValueError("Upload visuals only; the original narration is added locally")
        stream = streams[0]
        length = float(stream.get("duration", 0))
        ratio = stream.get("avg_frame_rate", "0/1").split("/")
        try:
            fps = float(ratio[0]) / float(ratio[1])
        except (ValueError, ZeroDivisionError, IndexError):
            fps = 0
        if (
            stream.get("codec_name") != "h264"
            or stream.get("width") != 1280
            or stream.get("height") != 720
            or (not math.isfinite(fps) or abs(fps - 30) > max(0.05, 1 / duration))
            or not math.isfinite(length)
            or abs(length - duration) > 0.06
        ):
            raise ValueError("Video frames do not match the recording")
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-nostdin",
                "-y",
                "-protocol_whitelist",
                "file,pipe",
                "-enable_drefs",
                "0",
                "-f",
                "mov",
                "-i",
                str(path / "visuals.mp4"),
                "-i",
                str(path / "original.wav"),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-ar",
                "48000",
                "-t",
                str(duration),
                "-movflags",
                "+faststart",
                "-threads",
                "1",
                str(path / "report.mp4"),
            ],
            check=True,
            timeout=120,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        output = (path / "report.mp4").read_bytes()
        if len(output) > MAX_VIDEO_BYTES:
            raise ValueError("Finished video exceeds 128 MB")
        return output
