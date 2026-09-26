from __future__ import annotations

import hashlib
import io
import json
import wave

import pytest

from omnigent.server.generated_response_audio_timings import (
    timings_artifact_key,
    validate_timing_sidecar,
)


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        target.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


def _sidecar(audio: bytes, narration: str = "Hello world.") -> dict[str, object]:
    return {
        "schema_version": 1,
        "engine": "kokoro",
        "audio_sha256": hashlib.sha256(audio).hexdigest(),
        "narration_sha256": hashlib.sha256(narration.encode("utf-8")).hexdigest(),
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


def test_validates_pair_hashes_units_and_sidecar_key() -> None:
    audio = _wav_bytes()
    sidecar = _sidecar(audio)
    result = validate_timing_sidecar(
        json.dumps(sidecar).encode(),
        audio=audio,
        narration="Hello world.",
        expected_duration_seconds=0.1,
    )
    assert result["position_unit"] == "unicode-code-point"
    assert [unit["text"] for unit in result["units"]] == ["Hello", "world"]
    assert timings_artifact_key("generated-response-audio/1/hash.wav") == (
        "generated-response-audio/1/hash.wav.timings.json"
    )


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda data: data.update(audio_sha256="0" * 64), "different audio"),
        (lambda data: data.update(narration_sha256="0" * 64), "different narration"),
        (lambda data: data.update(duration_seconds=float("nan")), "duration"),
        (
            lambda data: data["units"][1].update(start_seconds=0.02),
            "monotonic",
        ),
        (
            lambda data: data["units"][1].update(narration_start=0, narration_end=5),
            "narration positions",
        ),
    ],
)
def test_rejects_stale_or_malformed_metadata(mutate, message: str) -> None:
    audio = _wav_bytes()
    sidecar = _sidecar(audio)
    mutate(sidecar)
    with pytest.raises(ValueError, match=message):
        validate_timing_sidecar(
            json.dumps(sidecar).encode(),
            audio=audio,
            narration="Hello world.",
        )


def test_rejects_invalid_json_and_wrong_narration_span() -> None:
    audio = _wav_bytes()
    with pytest.raises(ValueError, match="valid JSON"):
        validate_timing_sidecar(b"{", audio=audio, narration="Hello world.")

    sidecar = _sidecar(audio)
    sidecar["units"][0]["text"] = "stale"
    with pytest.raises(ValueError, match="does not match"):
        validate_timing_sidecar(
            json.dumps(sidecar).encode(),
            audio=audio,
            narration="Hello world.",
        )
