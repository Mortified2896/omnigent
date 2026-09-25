"""Narration and asynchronous response audio post-processing tests."""

from __future__ import annotations

import asyncio
import io
import wave
from types import SimpleNamespace

from omnigent.server.generated_response_audio import (
    GeneratedResponseAudioCoordinator,
    _AudioWork,
    clean_narration,
)


def _wav_bytes() -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(24_000)
        target.writeframes(b"\x00\x00" * 2400)
    return buffer.getvalue()


def test_clean_narration_removes_markdown_urls_citations_and_code() -> None:
    source = """# Section 1 — Stack

- Read [release notes](https://example.com/release) [1].
- Live search is enabled.

```python
print('ignore this')
```
"""

    spoken = clean_narration(source)

    assert "Section 1: Stack." in spoken
    assert "Read release notes." in spoken
    assert "https://" not in spoken
    assert "[1]" not in spoken
    assert "print" not in spoken


def test_audio_worker_stores_wav_for_exact_completed_response() -> None:
    class AudioStore:
        def __init__(self) -> None:
            self.row = SimpleNamespace(
                status="pending",
                voice_profile="daily-brief",
                artifact_key=None,
                duration_seconds=None,
                sample_rate=None,
                error_code=None,
            )

        def claim_pending(self, conversation_id: str, response_id: str) -> bool:
            if self.row.status != "pending":
                return False
            self.row.status = "processing"
            return True

        def get(self, conversation_id: str, response_id: str):
            return self.row

        def mark_ready(self, conversation_id: str, response_id: str, **kwargs: object) -> bool:
            self.row.status = "ready"
            for key, value in kwargs.items():
                setattr(self.row, key, value)
            return True

        def mark_failed(self, conversation_id: str, response_id: str, error_code: str) -> bool:
            self.row.status = "failed"
            self.row.error_code = error_code
            return True

    class ConversationStore:
        def list_items(self, **kwargs: object):
            item = SimpleNamespace(
                status="completed",
                response_id="answer-17",
                data=SimpleNamespace(
                    role="assistant",
                    content=[
                        {
                            "type": "output_text",
                            "text": "A useful answer with current findings.",
                        }
                    ],
                ),
            )
            return SimpleNamespace(data=[item])

    class ArtifactStore:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def exists(self, key: str) -> bool:
            return key in self.values

        def get(self, key: str) -> bytes:
            return self.values[key]

        def put(self, key: str, payload: bytes) -> None:
            self.values[key] = payload

    class TtsResponse:
        status_code = 200
        headers = {
            "x-audio-duration-seconds": "0.1",
            "x-audio-sample-rate": "24000",
        }
        content = _wav_bytes()

    class TtsClient:
        async def post(self, url: str, *, json: dict[str, str]):
            assert url == "http://tts/v1/audio/speech"
            assert json["voice_profile"] == "daily-brief"
            assert "current findings" in json["text"]
            return TtsResponse()

    audio_store = AudioStore()
    artifact_store = ArtifactStore()
    coordinator = GeneratedResponseAudioCoordinator(
        audio_store=audio_store,
        conversation_store=ConversationStore(),
        artifact_store=artifact_store,
        tts_url="http://tts",
    )
    coordinator._client = TtsClient()  # type: ignore[assignment]

    asyncio.run(coordinator._process(_AudioWork(0, "conversation-3", "answer-17")))

    assert audio_store.row.status == "ready"
    assert audio_store.row.artifact_key in artifact_store.values
    assert artifact_store.values[audio_store.row.artifact_key] == _wav_bytes()
    assert audio_store.row.sample_rate == 24_000


def test_audio_failure_keeps_text_and_next_response_can_generate_audio() -> None:
    class AudioStore:
        def __init__(self) -> None:
            self.rows: dict[tuple[str, str], SimpleNamespace] = {}

        def create_pending(self, conversation_id: str, response_id: str, voice_profile: str):
            key = (conversation_id, response_id)
            if key not in self.rows:
                self.rows[key] = SimpleNamespace(
                    status="pending",
                    voice_profile=voice_profile,
                    artifact_key=None,
                    duration_seconds=None,
                    sample_rate=None,
                    error_code=None,
                )
            return self.rows[key]

        def claim_pending(self, conversation_id: str, response_id: str) -> bool:
            row = self.rows[(conversation_id, response_id)]
            if row.status != "pending":
                return False
            row.status = "processing"
            return True

        def get(self, conversation_id: str, response_id: str):
            return self.rows.get((conversation_id, response_id))

        def mark_failed(self, conversation_id: str, response_id: str, error_code: str) -> bool:
            row = self.rows[(conversation_id, response_id)]
            row.status = "failed"
            row.error_code = error_code
            return True

        def mark_ready(self, conversation_id: str, response_id: str, **kwargs: object) -> bool:
            row = self.rows[(conversation_id, response_id)]
            row.status = "ready"
            for key, value in kwargs.items():
                setattr(row, key, value)
            return True

    class ConversationStore:
        response_id = "answer-1"
        text = "This written response remains available when audio fails."

        def list_items(self, **kwargs: object):
            item = SimpleNamespace(
                status="completed",
                response_id=self.response_id,
                data=SimpleNamespace(
                    role="assistant",
                    content=[{"type": "output_text", "text": self.text}],
                ),
            )
            return SimpleNamespace(data=[item])

    class ArtifactStore:
        def __init__(self) -> None:
            self.values: dict[str, bytes] = {}

        def exists(self, key: str) -> bool:
            return key in self.values

        def get(self, key: str) -> bytes:
            return self.values[key]

        def put(self, key: str, payload: bytes) -> None:
            self.values[key] = payload

    class TtsResponse:
        def __init__(self, status_code: int) -> None:
            self.status_code = status_code
            self.headers = {
                "x-audio-duration-seconds": "0.1",
                "x-audio-sample-rate": "24000",
            }
            self.content = _wav_bytes()

    class TtsClient:
        def __init__(self) -> None:
            self.statuses = [503, 200]

        async def post(self, url: str, *, json: dict[str, str]):
            assert url == "http://tts/v1/audio/speech"
            return TtsResponse(self.statuses.pop(0))

    audio_store = AudioStore()
    conversation_store = ConversationStore()
    artifact_store = ArtifactStore()
    client = TtsClient()
    coordinator = GeneratedResponseAudioCoordinator(
        audio_store=audio_store,
        conversation_store=conversation_store,
        artifact_store=artifact_store,
        tts_url="http://tts",
    )
    coordinator._client = client  # type: ignore[assignment]

    audio_store.create_pending("conversation-4", "answer-1", "daily-brief")
    asyncio.run(coordinator._process(_AudioWork(0, "conversation-4", "answer-1")))

    failed = audio_store.get("conversation-4", "answer-1")
    assert failed.status == "failed"
    assert failed.error_code == "tts_http_503"
    assert conversation_store.text == "This written response remains available when audio fails."
    assert artifact_store.values == {}
    # A failed row for the same response stays terminal. A later scheduled
    # response gets its own row and can generate audio when TTS recovers.
    failed_retry = audio_store.create_pending("conversation-4", "answer-1", "daily-brief")
    assert failed_retry.status == "failed"
    conversation_store.response_id = "answer-2"
    audio_store.create_pending("conversation-4", "answer-2", "daily-brief")
    asyncio.run(coordinator._process(_AudioWork(0, "conversation-4", "answer-2")))

    recovered = audio_store.get("conversation-4", "answer-2")
    assert recovered.status == "ready"
    assert recovered.artifact_key in artifact_store.values
    assert artifact_store.values[recovered.artifact_key] == _wav_bytes()


def test_synthesis_timeout_covers_long_briefs_below_stale_lease():
    async def check():
        coordinator = GeneratedResponseAudioCoordinator(
            audio_store=SimpleNamespace(
                recover_processing=lambda: 0, list_pending_all_workspaces=list
            ),
            conversation_store=None,
            artifact_store=None,
            tts_url="http://tts",
        )
        await coordinator.start()
        try:
            assert coordinator._client.timeout.read == 1500.0
            assert coordinator._client.timeout.read < 30 * 60
        finally:
            await coordinator.stop()

    asyncio.run(check())
