"""Expiry removes only audio, preserves unrelated artifacts, and recovers safely."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from omnigent.server.generated_audio_playback import mobile_playback_copy
from omnigent.server.generated_audio_retention import expire_generated_audio
from omnigent.stores.artifact_store.local import LocalArtifactStore
from omnigent.stores.generated_response_audio import SqlAlchemyGeneratedResponseAudioStore


def test_expiry_preserves_recent_and_active_audio_and_does_not_resynthesize(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 1_000_000
    monkeypatch.setattr(
        "omnigent.stores.generated_response_audio.now_epoch", lambda: now - 8 * 86400
    )
    store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path / "artifacts"))
    cid = "a" * 32
    key = "generated-response-audio/0/" + "b" * 64 + ".wav"
    store.create_pending(cid, "expired", "elevenlabs:daniel")
    store.claim_pending(cid, "expired")
    store.mark_ready(cid, "expired", artifact_key=key, duration_seconds=1, sample_rate=24000)
    source = b"exact recording"
    playback = f"generated-response-audio/playback/{hashlib.sha256(source).hexdigest()}.mp3-v1"
    for name in [key, key + ".timings.json", playback, "reports/keep.md"]:
        artifacts.put(name, source)
    image_id = "f" * 64
    video_files = [key + ".video.mp4", key + ".video.mp4.json", key + ".video-image-" + image_id]
    for name in video_files:
        artifacts.put(name, source)
    artifacts.put(
        key + ".video-assets.json", json.dumps({image_id: {}, "reports/keep.md": {}}).encode()
    )
    monkeypatch.setattr("omnigent.stores.generated_response_audio.now_epoch", lambda: now)
    store.create_pending(cid, "recent", "elevenlabs:daniel")
    store.claim_pending(cid, "recent")
    fresh_key = "generated-response-audio/0/" + "c" * 64 + ".wav"
    store.mark_ready(cid, "recent", artifact_key=fresh_key, duration_seconds=1, sample_rate=24000)
    artifacts.put(fresh_key, source)
    store.create_pending(cid, "active", "elevenlabs:daniel")
    store.claim_pending(cid, "active")

    assert expire_generated_audio(store, artifacts, 7, now=now) == 1
    assert expire_generated_audio(store, artifacts, 7, now=now) == 0
    assert all(not artifacts.exists(name) for name in [key, key + ".timings.json", playback])
    assert all(not artifacts.exists(name) for name in [*video_files, key + ".video-assets.json"])
    assert artifacts.get("reports/keep.md") == source
    assert artifacts.exists(fresh_key)
    assert store.get(cid, "recent").status == "ready"
    assert store.get(cid, "active").status == "processing"
    expired = store.create_pending(cid, "expired", "elevenlabs:daniel")
    assert expired.status == "failed" and expired.error_code == "audio_expired"
    assert expired.artifact_key is None
    assert store.list_pending_all_workspaces() == []


def test_cleanup_resumes_after_file_delete_failure(
    db_uri: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("omnigent.stores.generated_response_audio.now_epoch", lambda: 100)
    store = SqlAlchemyGeneratedResponseAudioStore(db_uri)
    artifacts = LocalArtifactStore(str(tmp_path))
    cid = "d" * 32
    key = "generated-response-audio/0/" + "e" * 64 + ".wav"
    store.create_pending(cid, "answer", "elevenlabs:daniel")
    store.claim_pending(cid, "answer")
    store.mark_ready(cid, "answer", artifact_key=key, duration_seconds=1, sample_rate=24000)
    artifacts.put(key, b"audio")
    remove = artifacts.delete

    def fail_once(name: str) -> None:
        if name == key:
            raise OSError("temporary failure")
        remove(name)

    monkeypatch.setattr(artifacts, "delete", fail_once)
    assert expire_generated_audio(store, artifacts, 7, now=1_000_000) == 0
    assert store.get(cid, "answer").error_code == "audio_expiring"
    assert not store.retry_failed(cid, "answer")
    monkeypatch.setattr(artifacts, "delete", remove)
    assert expire_generated_audio(store, artifacts, 7, now=1_000_001) == 1
    assert store.get(cid, "answer").error_code == "audio_expired"
    assert not artifacts.exists(key)


def test_expired_download_cannot_recreate_a_playback_copy(tmp_path: Path) -> None:
    artifacts = LocalArtifactStore(str(tmp_path))
    with pytest.raises(ValueError, match="expired"):
        mobile_playback_copy(artifacts, b"old cached download", is_current=lambda: False)
    assert list(tmp_path.iterdir()) == []
