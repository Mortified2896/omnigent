"""Asynchronous post-processing for response audio on opted-in task runs."""

from __future__ import annotations

import asyncio
import hashlib
import html
import io
import logging
import os
import re
import wave
from contextlib import suppress
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.db.db_models import current_workspace_id, workspace_scope

_logger = logging.getLogger(__name__)
_URL_RE = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)
_LINK_RE = re.compile(r"!?\[([^\]]*)\]\((?:[^()\s]+|\([^)]*\))(?:\s+[^)]*)?\)")
_FOOTNOTE_RE = re.compile(r"^\s*\[\^?\d+\]:.*$", re.MULTILINE)
_CITATION_RE = re.compile(r"\[(?:\^?\d+|source(?:s)?|citation(?:s)?)\]", re.IGNORECASE)
_CODE_BLOCK_RE = re.compile(r"```[^\n]*\n.*?```", re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_DECORATION_RE = re.compile(r"(?<!\\)[*_~]{1,3}")
_MAX_NARRATION_CHARACTERS = int(os.getenv("OMNIGENT_TTS_MAX_NARRATION_CHARACTERS", "12000"))


def _ensure_sentence(text: str) -> str:
    value = text.strip()
    if value and value[-1] not in ".!?…。！？":
        return value + "."
    return value


def clean_narration(markdown: str, *, maximum: int = _MAX_NARRATION_CHARACTERS) -> str:
    """Derive speech text without changing the persisted Markdown response."""
    text = _CODE_BLOCK_RE.sub(" ", markdown)
    text = _FOOTNOTE_RE.sub(" ", text)
    text = _LINK_RE.sub(lambda match: match.group(1), text)
    text = _CITATION_RE.sub("", text)
    text = _HTML_TAG_RE.sub(" ", text)
    text = re.sub(r"`([^`]+)`", r"\1", text)
    text = _MARKDOWN_DECORATION_RE.sub("", text)
    text = html.unescape(text)
    text = _URL_RE.sub("", text)

    sentences: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if re.fullmatch(r"\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)+\|?", line):
            continue
        line = re.sub(r"^[>#]+\s*", "", line)
        line = re.sub(r"^\|\s*|\s*\|$", "", line)
        line = line.replace("|", ", ")
        bullet = re.match(r"^(?:[-+*]|\d+[.)])\s+", line)
        if bullet:
            line = line[bullet.end() :]
        heading = re.match(
            r"^(?:#{1,6}\s*)?(SECTION\s+\d+)\s*(?:[—–]|-|:)?\s*(.*)$",
            line,
            re.IGNORECASE,
        )
        if heading:
            section = f"{heading.group(1).title()}"
            title = heading.group(2).strip()
            line = f"{section}: {title}" if title else section
        elif raw_line.lstrip().startswith("#"):
            line = f"Next: {line}"
        line = re.sub(r"\s+", " ", line).strip()
        if line:
            sentences.append(_ensure_sentence(line))

    result = re.sub(r"\s+", " ", " ".join(sentences)).strip()
    result = re.sub(r"\s+([.,!?;:])", r"\1", result)
    if len(result) > maximum:
        excerpt = result[:maximum]
        boundary = max(excerpt.rfind(mark) for mark in (". ", "! ", "? ", "。", "！", "？"))
        if boundary > maximum // 2:
            excerpt = excerpt[: boundary + 1]
        else:
            cut = excerpt.rfind(" ")
            excerpt = excerpt[: cut if cut > maximum // 2 else maximum]
        result = excerpt.rstrip() + " Additional written content is omitted from this audio."
    return result


def _assistant_text(item: Any) -> str:
    data = getattr(item, "data", None)
    if getattr(data, "role", None) != "assistant":
        return ""
    if getattr(data, "is_meta", False):
        return ""
    content = getattr(data, "content", None) or []
    pieces: list[str] = []
    for block in content:
        if isinstance(block, dict):
            block_type = block.get("type")
            text = block.get("text")
        else:
            block_type = getattr(block, "type", None)
            text = getattr(block, "text", None)
        if block_type in {"output_text", "text"} and isinstance(text, str):
            pieces.append(text)
    return "\n".join(pieces)


def _response_narration(conversation_store: Any, conversation_id: str) -> tuple[str | None, str]:
    items_page = conversation_store.list_items(
        conversation_id=conversation_id,
        limit=1000,
        order="desc",
    )
    items = getattr(items_page, "data", items_page)
    response_id = None
    for item in items:
        if getattr(item, "status", None) != "completed":
            continue
        if _assistant_text(item).strip():
            response_id = getattr(item, "response_id", None)
            break
    if not isinstance(response_id, str) or not response_id:
        return None, ""
    chunks = [
        _assistant_text(item)
        for item in reversed(items)
        if getattr(item, "response_id", None) == response_id
    ]
    return response_id, clean_narration("\n".join(chunk for chunk in chunks if chunk))


@dataclass(frozen=True)
class _AudioWork:
    workspace_id: int
    conversation_id: str
    response_id: str


class GeneratedResponseAudioCoordinator:
    """One background worker that generates and stores opted-in response audio."""

    def __init__(
        self,
        *,
        audio_store: Any,
        conversation_store: Any,
        artifact_store: Any,
        tts_url: str | None,
    ) -> None:
        self.audio_store = audio_store
        self.conversation_store = conversation_store
        self.artifact_store = artifact_store
        self.tts_url = tts_url.rstrip("/") if tts_url else None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[_AudioWork] | None = None
        self._worker: asyncio.Task[None] | None = None
        self._client: httpx.AsyncClient | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._queue = asyncio.Queue()
        # Full local 1.7B narration can exceed 15 minutes at the supported
        # 12k-character ceiling. Stay below the store's 30-minute stale lease.
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(1500.0, connect=5.0))
        await asyncio.to_thread(self.audio_store.recover_processing)
        for row in await asyncio.to_thread(self.audio_store.list_pending_all_workspaces):
            self._queue.put_nowait(
                _AudioWork(row.workspace_id, row.conversation_id, row.response_id)
            )
        self._worker = asyncio.create_task(self._run(), name="generated-response-audio")

    async def stop(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
        if self._client is not None:
            await self._client.aclose()

    def enqueue_completed_scheduled_response(self, conversation_id: str) -> None:
        """Persist a pending row and queue work after a successful scheduled turn."""
        conversation = self.conversation_store.get_conversation(conversation_id)
        if conversation is None:
            return
        state = conversation.session_state or {}
        if state.get("scheduled_task_audio_enabled") is not True:
            return
        voice_profile = state.get("scheduled_task_audio_voice_profile")
        if not isinstance(voice_profile, str) or not voice_profile:
            _logger.warning(
                "Scheduled session %s opted into audio without a voice profile", conversation_id
            )
            return
        response_id, narration = _response_narration(self.conversation_store, conversation_id)
        if response_id is None or not narration:
            return
        entry = self.audio_store.create_pending(conversation_id, response_id, voice_profile)
        if entry.status in {"ready", "failed", "processing"}:
            return
        self._enqueue(_AudioWork(current_workspace_id(), conversation_id, response_id))

    def _enqueue(self, work: _AudioWork) -> None:
        loop, queue = self._loop, self._queue
        if loop is not None and queue is not None and not loop.is_closed():
            loop.call_soon_threadsafe(queue.put_nowait, work)

    async def _run(self) -> None:
        assert self._queue is not None
        while True:
            work = await self._queue.get()
            try:
                await self._process(work)
            except asyncio.CancelledError:
                raise
            except Exception:
                _logger.exception(
                    "Unexpected generated-audio worker error for conversation=%s response=%s",
                    work.conversation_id,
                    work.response_id,
                )
            finally:
                self._queue.task_done()

    @staticmethod
    def _artifact_key(work: _AudioWork) -> str:
        digest = hashlib.sha256(
            f"{work.workspace_id}:{work.conversation_id}:{work.response_id}".encode()
        ).hexdigest()
        return f"generated-response-audio/{work.workspace_id}/{digest}.wav"

    def _read_existing_audio(self, key: str) -> tuple[bytes, float, int] | None:
        if not self.artifact_store.exists(key):
            return None
        payload = self.artifact_store.get(key)
        try:
            with wave.open(io.BytesIO(payload), "rb") as audio:
                rate = audio.getframerate()
                duration = audio.getnframes() / rate
        except (wave.Error, EOFError, ZeroDivisionError):
            return None
        return payload, round(duration, 2), rate

    async def _process(self, work: _AudioWork) -> None:
        with workspace_scope(work.workspace_id):
            claimed = await asyncio.to_thread(
                self.audio_store.claim_pending,
                work.conversation_id,
                work.response_id,
            )
            if not claimed:
                return
            key = self._artifact_key(work)
            try:
                existing = await asyncio.to_thread(self._read_existing_audio, key)
                if existing is not None:
                    _, duration, rate = existing
                    await asyncio.to_thread(
                        self.audio_store.mark_ready,
                        work.conversation_id,
                        work.response_id,
                        artifact_key=key,
                        duration_seconds=duration,
                        sample_rate=rate,
                    )
                    return
                if not self.tts_url:
                    raise _PostprocessError("tts_not_configured")
                response_id, narration = await asyncio.to_thread(
                    _response_narration,
                    self.conversation_store,
                    work.conversation_id,
                )
                if response_id != work.response_id or not narration:
                    raise _PostprocessError("narration_unavailable")
                entry = await asyncio.to_thread(
                    self.audio_store.get,
                    work.conversation_id,
                    work.response_id,
                )
                if entry is None:
                    raise _PostprocessError("audio_record_missing")
                assert self._client is not None
                result = await self._client.post(
                    f"{self.tts_url}/v1/audio/speech",
                    json={
                        "text": narration,
                        "voice_profile": entry.voice_profile,
                        "language": "English",
                    },
                )
                if result.status_code != 200:
                    raise _PostprocessError(f"tts_http_{result.status_code}")
                try:
                    duration = float(result.headers["x-audio-duration-seconds"])
                    rate = int(result.headers["x-audio-sample-rate"])
                except (KeyError, ValueError) as exc:
                    raise _PostprocessError("tts_invalid_audio_metadata") from exc
                payload = result.content
                await asyncio.to_thread(self.artifact_store.put, key, payload)
                await asyncio.to_thread(
                    self.audio_store.mark_ready,
                    work.conversation_id,
                    work.response_id,
                    artifact_key=key,
                    duration_seconds=duration,
                    sample_rate=rate,
                )
            except _PostprocessError as exc:
                await asyncio.to_thread(
                    self.audio_store.mark_failed,
                    work.conversation_id,
                    work.response_id,
                    exc.code,
                )
            except Exception as exc:
                _logger.exception(
                    "Audio post-processing failed for conversation=%s response=%s (%s)",
                    work.conversation_id,
                    work.response_id,
                    type(exc).__name__,
                )
                await asyncio.to_thread(
                    self.audio_store.mark_failed,
                    work.conversation_id,
                    work.response_id,
                    "tts_unavailable",
                )


class _PostprocessError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code
