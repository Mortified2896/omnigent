"""Integrity checks for optional word timings attached to generated audio."""

from __future__ import annotations

import hashlib
import json
import math
import re
import unicodedata
from typing import Any


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MAX_SIDECAR_BYTES = 8 * 1024 * 1024
_MAX_UNITS = 100_000


def timings_artifact_key(audio_artifact_key: str) -> str:
    """Return the co-located timing sidecar key for an audio artifact."""
    return f"{audio_artifact_key}.timings.json"


def _word_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return "".join(character for character in normalized if character.isalnum())


def validate_timing_sidecar(
    payload: bytes,
    *,
    audio: bytes,
    narration: str,
    expected_duration_seconds: float | None = None,
) -> dict[str, Any]:
    """Validate a version-1 timing sidecar against its exact WAV and narration.

    Raises ``ValueError`` on malformed, stale, or unsafe metadata. Callers must
    treat that error as loss of highlighting only; audio remains playable.
    Narration positions are Unicode code-point offsets into the normalized
    spoken narration sent to the TTS backend.
    """
    if len(payload) > _MAX_SIDECAR_BYTES:
        raise ValueError("timing sidecar is too large")
    try:
        data = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("timing sidecar is not valid JSON") from exc
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError("unsupported timing sidecar schema")
    if not isinstance(data.get("engine"), str) or not data["engine"]:
        raise ValueError("timing sidecar has no engine")

    audio_hash = data.get("audio_sha256")
    narration_hash = data.get("narration_sha256")
    if not isinstance(audio_hash, str) or not _SHA256_RE.fullmatch(audio_hash):
        raise ValueError("timing sidecar has an invalid audio hash")
    if not isinstance(narration_hash, str) or not _SHA256_RE.fullmatch(narration_hash):
        raise ValueError("timing sidecar has an invalid narration hash")
    if hashlib.sha256(audio).hexdigest() != audio_hash:
        raise ValueError("timing sidecar belongs to different audio")
    if hashlib.sha256(narration.encode("utf-8")).hexdigest() != narration_hash:
        raise ValueError("timing sidecar belongs to different narration")

    duration = data.get("duration_seconds")
    if isinstance(duration, bool) or not isinstance(duration, (int, float)):
        raise ValueError("timing sidecar has an invalid duration")
    duration = float(duration)
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("timing sidecar has an invalid duration")
    if expected_duration_seconds is not None and abs(duration - expected_duration_seconds) > 0.05:
        raise ValueError("timing sidecar duration differs from the audio row")

    units = data.get("units")
    if not isinstance(units, list) or not units or len(units) > _MAX_UNITS:
        raise ValueError("timing sidecar has an invalid unit list")
    previous_start = -1.0
    previous_end = -1.0
    previous_narration_end = -1
    validated_units: list[dict[str, Any]] = []
    for unit in units:
        if not isinstance(unit, dict):
            raise ValueError("timing unit is not an object")
        text = unit.get("text")
        narration_start = unit.get("narration_start")
        narration_end = unit.get("narration_end")
        start = unit.get("start_seconds")
        end = unit.get("end_seconds")
        if not isinstance(text, str) or not text.strip():
            raise ValueError("timing unit has no text")
        if (
            isinstance(narration_start, bool)
            or not isinstance(narration_start, int)
            or isinstance(narration_end, bool)
            or not isinstance(narration_end, int)
            or narration_start < 0
            or narration_end <= narration_start
            or narration_end > len(narration)
            or narration_start < previous_narration_end
        ):
            raise ValueError("timing unit has invalid narration positions")
        source_text = narration[narration_start:narration_end]
        if not _word_key(source_text) or _word_key(source_text) != _word_key(text):
            raise ValueError("timing unit text does not match its narration span")
        if (
            isinstance(start, bool)
            or not isinstance(start, (int, float))
            or isinstance(end, bool)
            or not isinstance(end, (int, float))
        ):
            raise ValueError("timing unit has invalid timestamps")
        start = float(start)
        end = float(end)
        if (
            not math.isfinite(start)
            or not math.isfinite(end)
            or start < 0
            or end <= start
            or end > duration + 0.025
            or start < previous_start
            or start < previous_end - 0.025
        ):
            raise ValueError("timing timestamps are not monotonic or exceed the audio")
        previous_start = start
        previous_end = end
        previous_narration_end = narration_end
        validated_units.append(
            {
                "text": text,
                "narration_start": narration_start,
                "narration_end": narration_end,
                "start_seconds": start,
                "end_seconds": end,
            }
        )

    return {
        "schema_version": 1,
        "engine": data["engine"],
        "audio_sha256": audio_hash,
        "narration_sha256": narration_hash,
        "duration_seconds": duration,
        "position_unit": "unicode-code-point",
        "units": validated_units,
    }


def validate_timing_sidecar_bytes(
    payload: bytes,
    *,
    audio: bytes,
    narration: str,
    expected_duration_seconds: float | None = None,
) -> bytes:
    """Return canonical JSON bytes after validating a sidecar."""
    data = validate_timing_sidecar(
        payload,
        audio=audio,
        narration=narration,
        expected_duration_seconds=expected_duration_seconds,
    )
    return json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
