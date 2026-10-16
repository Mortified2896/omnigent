"""Eleven v4 Turbo narration with WAV audio and original-text word timings."""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import io
import json
import math
import re
import wave
from collections.abc import Awaitable, Callable
from typing import Any

from websockets.exceptions import WebSocketException
from websockets.legacy.client import connect
from websockets.legacy.exceptions import InvalidStatusCode

ELEVENLABS_MODEL = "eleven_v4_turbo"
ELEVENLABS_VOICE_PREFIX = "elevenlabs:"
_SAMPLE_RATE = 24_000
_CHUNK_LIMIT = 2_000


class ElevenLabsSpeechError(Exception):
    """A safe failure code, without provider response bodies or credentials."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _chunks(text: str) -> list[str]:
    chunks = []
    while len(text) > _CHUNK_LIMIT:
        boundary = text.rfind(". ", _CHUNK_LIMIT // 2, _CHUNK_LIMIT)
        if boundary < 0:
            boundary = text.rfind(" ", _CHUNK_LIMIT // 2, _CHUNK_LIMIT)
        end = boundary + 1 if boundary >= 0 else _CHUNK_LIMIT
        chunks.append(text[:end])
        text = text[end:]
    if text:
        chunks.append(text)
    return chunks


def _units(
    alignment: Any, text: str, narration_offset: int, time_offset: float, duration: float
) -> list[dict[str, Any]] | None:
    if not isinstance(alignment, dict):
        return None
    characters = alignment.get("characters")
    starts = alignment.get("character_start_times_seconds")
    ends = alignment.get("character_end_times_seconds")
    if (
        not isinstance(characters, list)
        or not isinstance(starts, list)
        or not isinstance(ends, list)
    ):
        return None
    if characters != list(text) or len(starts) != len(text) or len(ends) != len(text):
        return None
    if any(
        isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value)
        for value in [*starts, *ends]
    ):
        return None
    # Provider alignment can extend slightly past the final PCM sample (80 ms
    # observed on v4). Clip only a short tail; larger inconsistencies lose timings.
    if any(end > duration + 0.2 for end in ends):
        return None
    try:
        return [
            {
                "text": match.group(),
                "narration_start": narration_offset + match.start(),
                "narration_end": narration_offset + match.end(),
                "start_seconds": time_offset + float(starts[match.start()]),
                "end_seconds": time_offset + min(float(ends[match.end() - 1]), duration),
            }
            for match in re.finditer(r"\S+", text)
            if any(character.isalnum() for character in match.group())
        ]
    except (TypeError, ValueError, OverflowError):
        return None


async def _stream_chunk(api_key: str, voice_id: str, text: str) -> tuple[bytes, dict[str, Any]]:
    """Collect one flushed dialogue stream, requiring the provider's final frame."""
    pcm_parts = []
    alignment: dict[str, Any] = {
        "characters": [],
        "character_start_times_seconds": [],
        "character_end_times_seconds": [],
    }
    sample_count = 0
    alignment_offset = 0.0
    valid_alignment = True
    try:
        async with asyncio.timeout(180):
            async with connect(
                "wss://api.elevenlabs.io/v1/text-to-dialogue/stream-input"
                f"?model_id={ELEVENLABS_MODEL}&output_format=pcm_24000&sync_alignment=true",
                extra_headers={"xi-api-key": api_key},
                open_timeout=20,
                close_timeout=5,
                max_size=8 * 1024 * 1024,
            ) as socket:
                await socket.send(json.dumps({"voices": [voice_id]}))
                await socket.send(json.dumps({"inputs": [{"text": text, "voice_id": voice_id}]}))
                await socket.send(json.dumps({"close_socket": True}))
                while True:
                    try:
                        data = json.loads(await asyncio.wait_for(socket.recv(), timeout=60))
                        if not isinstance(data, dict):
                            raise ValueError("invalid frame")
                        if data.get("error"):
                            raise ElevenLabsSpeechError("elevenlabs_generation_failed")
                        pcm = base64.b64decode(data.get("audio") or "", validate=True)
                        if len(pcm) % 2:
                            raise ValueError("invalid PCM")
                    except (ValueError, TypeError, binascii.Error):
                        raise ElevenLabsSpeechError("elevenlabs_invalid_audio") from None
                    part = data.get("alignment")
                    if part is not None:
                        try:
                            chars = part["chars"]
                            starts = part["char_start_times_ms"]
                            durations = part["char_durations_ms"]
                            if not all(
                                isinstance(values, list) for values in (chars, starts, durations)
                            ):
                                raise ValueError("invalid alignment")
                            if len(chars) != len(starts) or len(chars) != len(durations):
                                raise ValueError("invalid alignment")
                            if any(
                                isinstance(value, bool)
                                or not isinstance(value, (int, float))
                                or not math.isfinite(value)
                                or value < 0
                                for value in [*starts, *durations]
                            ):
                                raise ValueError("invalid alignment")
                            # Alignment segments and PCM packets have independent
                            # boundaries. Advance on alignment durations, never on
                            # the size of the audio packet carrying the alignment.
                            offset = alignment_offset
                            alignment["characters"].extend(chars)
                            alignment["character_start_times_seconds"].extend(
                                offset + start / 1000 for start in starts
                            )
                            alignment["character_end_times_seconds"].extend(
                                offset + (start + duration) / 1000
                                for start, duration in zip(starts, durations, strict=True)
                            )
                            if starts:
                                alignment_offset += (
                                    max(
                                        start + duration
                                        for start, duration in zip(starts, durations, strict=True)
                                    )
                                    / 1000
                                )
                        except (KeyError, ValueError, TypeError):
                            valid_alignment = False
                    pcm_parts.append(pcm)
                    sample_count += len(pcm) // 2
                    if data.get("is_final") is True:
                        break
    except InvalidStatusCode as exc:
        raise ElevenLabsSpeechError(f"elevenlabs_http_{exc.status_code}") from None
    except (WebSocketException, OSError, TimeoutError):
        raise ElevenLabsSpeechError("elevenlabs_unavailable") from None
    if not sample_count:
        raise ElevenLabsSpeechError("elevenlabs_invalid_audio")
    return b"".join(pcm_parts), alignment if valid_alignment else {}


async def generate_elevenlabs_speech(
    *,
    api_key: str,
    voice_id: str,
    narration: str,
    on_progress: Callable[[int, int, int], Awaitable[None]] | None = None,
) -> tuple[bytes, bytes | None, float, int]:
    """Make one stream per chunk, with no automatic retry or model fallback."""
    if not api_key:
        raise ElevenLabsSpeechError("elevenlabs_key_missing")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", voice_id):
        raise ElevenLabsSpeechError("elevenlabs_voice_invalid")
    if not narration.strip():
        raise ElevenLabsSpeechError("narration_unavailable")
    pcm_parts: list[bytes] = []
    units: list[dict[str, Any]] = []
    complete_timings = True
    narration_offset = 0
    sample_count = 0
    chunks = _chunks(narration)
    for index, text in enumerate(chunks):
        pcm, alignment = await _stream_chunk(api_key, voice_id, text)
        chunk_units = _units(
            alignment,
            text,
            narration_offset,
            sample_count / _SAMPLE_RATE,
            len(pcm) / 2 / _SAMPLE_RATE,
        )
        if chunk_units is None:
            complete_timings = False
        else:
            units.extend(chunk_units)
        pcm_parts.append(pcm)
        sample_count += len(pcm) // 2
        narration_offset += len(text)
        if on_progress is not None:
            await on_progress(narration_offset, index + 1, len(chunks))
    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(_SAMPLE_RATE)
        for pcm in pcm_parts:
            wav.writeframes(pcm)
    payload = output.getvalue()
    duration = sample_count / _SAMPLE_RATE
    timings = None
    if complete_timings and units:
        timings = json.dumps(
            {
                "schema_version": 1,
                "engine": f"elevenlabs/{ELEVENLABS_MODEL}",
                "audio_sha256": hashlib.sha256(payload).hexdigest(),
                "narration_sha256": hashlib.sha256(narration.encode()).hexdigest(),
                "duration_seconds": duration,
                "units": units,
            },
            allow_nan=False,
        ).encode()
    return payload, timings, duration, _SAMPLE_RATE
