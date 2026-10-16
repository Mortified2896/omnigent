"""Provider contract, charge-safe failures, and original-narration alignment."""

from __future__ import annotations

import asyncio
import base64
import io
import json
import wave
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs, urlparse

import httpx
import pytest
from websockets.exceptions import ConnectionClosedError
from websockets.legacy.exceptions import InvalidStatusCode

from omnigent.server.elevenlabs_speech import (
    ElevenLabsSpeechError,
    _units,
    generate_elevenlabs_speech,
)
from omnigent.server.generated_response_audio import GeneratedResponseAudioCoordinator, _AudioWork
from omnigent.server.generated_response_audio_timings import validate_timing_sidecar


def _frame(text: str) -> dict[str, object]:
    return {
        "audio": base64.b64encode(b"\x00\x00" * 2400 * len(text)).decode(),
        "alignment": {
            "chars": list(text),
            "char_start_times_ms": [index * 100 for index in range(len(text))],
            "char_durations_ms": [100] * len(text),
        },
    }


def _mock_stream(monkeypatch, frames=None, status=None):
    sockets = []

    class Socket:
        def __init__(self):
            self.sent = []
            self.frames = []

        async def send(self, value):
            message = json.loads(value)
            self.sent.append(message)
            if message.get("close_socket"):
                assert self.sent[0] == {"voices": ["voice123"]}
                text = self.sent[1]["inputs"][0]["text"]
                assert self.sent[1]["inputs"][0]["voice_id"] == "voice123"
                assert self.sent[-1] == {"close_socket": True}
                self.frames = (
                    frames(text)
                    if frames
                    else [
                        _frame(text[:3]),
                        _frame(text[3:]),
                        {"is_final_audio_for_turn": True},
                        {"is_final": True},
                    ]
                )

        async def recv(self):
            if not self.frames:
                raise ConnectionClosedError(None, None)
            return json.dumps(self.frames.pop(0))

    @asynccontextmanager
    async def connect(uri, **kwargs):
        parsed = urlparse(uri)
        assert parsed.scheme == "wss"
        assert parsed.netloc == "api.elevenlabs.io"
        assert parsed.path == "/v1/text-to-dialogue/stream-input"
        assert parse_qs(parsed.query) == {
            "model_id": ["eleven_v4_turbo"],
            "output_format": ["pcm_24000"],
            "sync_alignment": ["true"],
        }
        assert kwargs["extra_headers"] == {"xi-api-key": "test-secret"}
        socket = Socket()
        sockets.append(socket)
        if status:
            raise InvalidStatusCode(status, {})
        yield socket

    monkeypatch.setattr("omnigent.server.elevenlabs_speech.connect", connect)
    return sockets


def test_turbo_stream_joins_split_words_and_offsets_timings_across_chunks(monkeypatch):
    monkeypatch.setattr("omnigent.server.elevenlabs_speech._CHUNK_LIMIT", 12)
    sockets = _mock_stream(monkeypatch)
    narration = "First news. Next news. Last news."
    progress = []

    async def note(characters, completed, total):
        progress.append((characters, completed, total))

    audio, timings, duration, rate = asyncio.run(
        generate_elevenlabs_speech(
            api_key="test-secret",
            voice_id="voice123",
            narration=narration,
            on_progress=note,
        )
    )
    assert len(sockets) > 1
    assert progress[-1] == (len(narration), len(sockets), len(sockets))
    assert "".join(socket.sent[1]["inputs"][0]["text"] for socket in sockets) == narration
    assert rate == 24000
    with wave.open(io.BytesIO(audio)) as wav:
        assert wav.getnframes() / wav.getframerate() == duration
    sidecar = validate_timing_sidecar(
        timings,
        audio=audio,
        narration=narration,
        expected_duration_seconds=duration,
    )
    assert sidecar["engine"] == "elevenlabs/eleven_v4_turbo"
    assert [unit["text"] for unit in sidecar["units"]] == narration.split()
    assert sidecar["units"][1]["start_seconds"] == pytest.approx(0.6)
    assert sidecar["units"][-1]["end_seconds"] == pytest.approx(len(narration) / 10)


@pytest.mark.parametrize("status", [401, 429, 503])
def test_provider_failure_is_not_retried_or_exposed(monkeypatch, status):
    sockets = _mock_stream(monkeypatch, status=status)
    with pytest.raises(ElevenLabsSpeechError, match=f"^elevenlabs_http_{status}$"):
        asyncio.run(
            generate_elevenlabs_speech(
                api_key="test-secret",
                voice_id="voice123",
                narration="News.",
            )
        )
    assert len(sockets) == 1


@pytest.mark.parametrize(
    "frames,code",
    [
        (lambda text: [_frame(text)], "elevenlabs_unavailable"),
        (
            lambda text: [_frame(text), {"error": "secret provider detail"}],
            "elevenlabs_generation_failed",
        ),
        (lambda text: [{"audio": "bad-base64"}], "elevenlabs_invalid_audio"),
        (lambda text: [{"is_final": True}], "elevenlabs_invalid_audio"),
    ],
)
def test_partial_and_invalid_audio_are_never_published_or_retried(monkeypatch, frames, code):
    sockets = _mock_stream(monkeypatch, frames=frames)
    with pytest.raises(ElevenLabsSpeechError, match=f"^{code}$"):
        asyncio.run(
            generate_elevenlabs_speech(
                api_key="test-secret",
                voice_id="voice123",
                narration="News.",
            )
        )
    assert len(sockets) == 1


def test_alignment_clock_is_independent_of_pcm_packet_boundaries(monkeypatch):
    def frames(text):
        first, second = _frame(text[:3]), _frame(text[3:])
        # Provider may attach a segment's alignment after its audio has already
        # arrived, including a split in the middle of a word.
        first["audio"] = base64.b64encode(b"\x00\x00" * 2400).decode()
        second["audio"] = base64.b64encode(b"\x00\x00" * 2400 * (len(text) - 1)).decode()
        return [first, second, {"is_final": True}]

    _mock_stream(monkeypatch, frames=frames)
    narration = "First news."
    audio, timings, duration, _ = asyncio.run(
        generate_elevenlabs_speech(
            api_key="test-secret",
            voice_id="voice123",
            narration=narration,
        )
    )
    sidecar = validate_timing_sidecar(
        timings,
        audio=audio,
        narration=narration,
        expected_duration_seconds=duration,
    )
    assert sidecar["units"][0]["end_seconds"] == pytest.approx(0.5)
    assert sidecar["units"][1]["start_seconds"] == pytest.approx(0.6)


@pytest.mark.parametrize("alignment", [None, {}, {"chars": ["wrong"]}])
def test_missing_or_invalid_stream_alignment_keeps_playable_audio(monkeypatch, alignment):
    def frames(text):
        frame = _frame(text)
        frame["alignment"] = alignment
        return [frame, {"is_final": True}]

    _mock_stream(monkeypatch, frames=frames)
    audio, timings, duration, rate = asyncio.run(
        generate_elevenlabs_speech(
            api_key="test-secret",
            voice_id="voice123",
            narration="News.",
        )
    )
    assert audio.startswith(b"RIFF") and duration > 0 and rate == 24000
    assert timings is None


def test_keychain_audio_runs_without_local_tts(monkeypatch: pytest.MonkeyPatch):
    _mock_stream(monkeypatch)
    secret_reads = []

    def load_secret(name: str):
        secret_reads.append(name)
        return "test-secret"

    monkeypatch.setattr("omnigent.onboarding.secrets.load_secret", load_secret)
    row = SimpleNamespace(voice_profile="elevenlabs:voice123", updated_at=1)
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
        async with httpx.AsyncClient() as client:
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
    assert any(call.args[0].endswith(".wav") for call in artifact_store.put.call_args_list)
    assert any(
        call.args[0].endswith(".generation.json") for call in artifact_store.put.call_args_list
    )


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
