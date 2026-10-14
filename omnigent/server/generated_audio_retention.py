"""Remove expired recordings and their copies without touching report text."""

from __future__ import annotations

import hashlib
import logging
import re
import time

from omnigent.server.generated_audio_playback import _encode_lock
from omnigent.server.generated_response_audio_timings import timings_artifact_key
from omnigent.stores.artifact_store import ArtifactStore
from omnigent.stores.generated_response_audio import SqlAlchemyGeneratedResponseAudioStore

_logger = logging.getLogger(__name__)
_AUDIO_KEY = re.compile(r"generated-response-audio/\d+/[a-f0-9]{64}\.wav")


def expire_generated_audio(
    audio_store: SqlAlchemyGeneratedResponseAudioStore,
    artifact_store: ArtifactStore,
    retention_days: int,
    *,
    now: int | None = None,
) -> int:
    """Only known ready audio expires; pending or active generation is preserved."""
    if retention_days <= 0:
        raise ValueError("Audio retention must be a positive number of days")
    cutoff = (int(time.time()) if now is None else now) - retention_days * 86400
    removed = 0
    for row in audio_store.list_expired_all_workspaces(cutoff):
        key = row.artifact_key
        if not key or not _AUDIO_KEY.fullmatch(key):
            continue
        if not audio_store.claim_expired(row):
            continue
        try:
            # Serialize with MP3 creation. Delete WAV last so an interrupted
            # cleanup can still derive the correct content-hash playback key.
            with _encode_lock:
                try:
                    source = artifact_store.get(key)
                except KeyError:
                    source = None
                if source is not None:
                    digest = hashlib.sha256(source).hexdigest()
                    artifact_store.delete(f"generated-response-audio/playback/{digest}.mp3-v1")
                artifact_store.delete(timings_artifact_key(key))
                artifact_store.delete(key)
            if audio_store.complete_expiry(row):
                removed += 1
        except (OSError, ValueError):
            _logger.exception("Audio cleanup interrupted for response=%s", row.response_id)
    if removed:
        _logger.info("Expired %s generated recordings older than %s days", removed, retention_days)
    return removed
