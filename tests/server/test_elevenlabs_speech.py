"""Provider contract, charge-safe failures, and original-narration alignment."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest

from omnigent.server.elevenlabs_speech import (
    ElevenLabsSpeechError,
    _units,
    generate_elevenlabs_speech,
)
from omnigent.server.generated_response_audio import GeneratedResponseAudioCoordinator, _AudioWork
from omnigent.server.generated_response_audio_timings import validate_timing_sidecar


def _response(text: str) -> dict[str, object]:
    return {
        "audio_base64": base64.b64encode(b"\x00\x00" * 2400 * len(text)).decode(),
        "alignment": {
            "characters": list(text),
            "character_start_times_seconds": [index / 10 for index in range(len(text))],
            "character_end_times_seconds": [(index + 1) / 10 for index in range(len(text))],
        },
    }


def test_quality_model_and_timings_remain_exact_across_chunks(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("omnigent.server.elevenlabs_speech._CHUNK_LIMIT", 12)
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        body = json.loads(request.content)
        assert body["model_id"] == "eleven_v4"
        assert "optimize_streaming_latency" not in request.url.params
        assert request.url.params["output_format"] == "pcm_24000"
        assert request.headers["xi-api-key"] == "test-secret"
        assert request.url.host == "api.elevenlabs.io"
        assert request.url.path == "/v1/text-to-dialogue/with-timestamps"
        assert body["inputs"][0]["voice_id"] == "voice123"
        return httpx.Response(200, json=_response(body["inputs"][0]["text"]))

    narration = "First news. Next news. Last news."

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await generate_elevenlabs_speech(
                client, api_key="test-secret", voice_id="voice123", narration=narration
            )

    audio, timings, duration, rate = asyncio.run(run())
    assert len(requests) > 1
    assert (
        "".join(json.loads(request.content)["inputs"][0]["text"] for request in requests)
        == narration
    )
    assert rate == 24000
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes() / wav.getframerate() == duration
    assert timings is not None
    sidecar = validate_timing_sidecar(
        timings, audio=audio, narration=narration, expected_duration_seconds=duration
    )
    assert [unit["text"] for unit in sidecar["units"]] == narration.split()


@pytest.mark.parametrize("status", [401, 429, 503])
def test_provider_failure_is_not_retried_or_exposed(status: int):
    calls = []

    def handler(request: httpx.Request):
        calls.append(request)
        return httpx.Response(status, json={"detail": "sensitive provider response"})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await generate_elevenlabs_speech(
                client, api_key="test-secret", voice_id="voice123", narration="News."
            )

    with pytest.raises(ElevenLabsSpeechError, match=f"^elevenlabs_http_{status}$"):
        asyncio.run(run())
    assert len(calls) == 1


def test_keychain_audio_runs_without_local_tts(monkeypatch: pytest.MonkeyPatch):
    secret_reads = []

    def load_secret(name: str):
        secret_reads.append(name)
        return "test-secret"

    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", load_secret)
    row = SimpleNamespace(voice_profile="elevenlabs:voice123")
    audio_store = Mock()
    audio_store.claim_pending.return_value = True
    audio_store.get.return_value = row
    artifact_store = Mock()
    artifact_store.exists.return_value = False
    conversation_store = Mock()
    conversation_store.list_items.return_value = SimpleNamespace(
        data=[
            SimpleNamespace(
                status="completed",
                response_id="answer123",
                data=SimpleNamespace(
                    role="assistant", content=[{"type": "output_text", "text": "News."}]
                ),
            )
        ]
    )

    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=_response("News."))
            )
        ) as client:
            coordinator = GeneratedResponseAudioCoordinator(
                audio_store=audio_store,
                conversation_store=conversation_store,
                artifact_store=artifact_store,
                tts_url=None,
            )
            coordinator._client = client
            await coordinator._process(_AudioWork(0, "conversation123", "answer123"))

    asyncio.run(run())
    assert secret_reads == ["elevenlabs"]
    audio_store.mark_failed.assert_not_called()
    audio_store.mark_ready.assert_called_once()
    assert audio_store.mark_ready.call_args.kwargs["sample_rate"] == 24000
    assert artifact_store.put.call_count == 2  # WAV and its validated word timings.


def test_v4_alignment_short_tail_is_bound_to_audio_and_large_mismatch_is_rejected():
    alignment = {
        "characters": list("Report."),
        "character_start_times_seconds": [0, 0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
        "character_end_times_seconds": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6, 0.78],
    }
    units = _units(alignment, "Report.", 0, 0, 0.7)
    assert units is not None and units[-1]["end_seconds"] == 0.7
    alignment["character_end_times_seconds"][-1] = 2.0
    assert _units(alignment, "Report.", 0, 0, 0.7) is None
