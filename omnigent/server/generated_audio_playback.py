"""Optional, cached mobile playback copies; the lossless source remains canonical."""

from __future__ import annotations

import hashlib
import subprocess
import tempfile
import threading
from pathlib import Path

from omnigent.stores.artifact_store import ArtifactStore

# Bound encoder CPU use and avoid duplicate work during simultaneous range probes.
_encode_lock = threading.Lock()


def mobile_playback_copy(store: ArtifactStore, source: bytes) -> bytes:
    """Return a source-hash-keyed MP3, or raise so the caller can serve the WAV.

    A seekable output file lets ffmpeg write gapless duration/delay metadata.
    No resynthesis, silence trimming, tempo changes, or alignment pass occurs.
    Timings continue to describe the canonical WAV's sample timeline.
    """
    digest = hashlib.sha256(source).hexdigest()
    key = f"generated-response-audio/playback/{digest}.mp3-v1"
    with _encode_lock:
        try:
            cached = store.get(key)
            if cached:
                return cached
        except KeyError:
            pass
        with tempfile.TemporaryDirectory(prefix="omnigent-audio-") as directory:
            output = Path(directory) / "playback.mp3"
            subprocess.run(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "wav",
                    "-i",
                    "pipe:0",
                    "-map",
                    "0:a:0",
                    "-map_metadata",
                    "-1",
                    "-c:a",
                    "libmp3lame",
                    "-b:a",
                    "64k",
                    "-threads",
                    "1",
                    str(output),
                ],
                input=source,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                check=True,
                timeout=60,
            )
            encoded = output.read_bytes()
        if not encoded:
            raise ValueError("Empty playback copy")
        store.put(key, encoded)
        return encoded
