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
