"""Small generation receipts and model-specific estimates; no narration or secrets."""

from __future__ import annotations

import json
import statistics
import time
from typing import Any

from omnigent.stores.artifact_store import ArtifactStore


def progress_key(audio_key: str) -> str:
    return audio_key + ".generation.json"


def read_receipt(store: ArtifactStore, key: str) -> dict[str, Any]:
    try:
        value = json.loads(store.get(key))
        return value if isinstance(value, dict) else {}
    except (KeyError, ValueError, OSError):
        return {}


def save_receipt(store: ArtifactStore, key: str, data: dict[str, Any]) -> None:
    store.put(key, json.dumps(data, allow_nan=False).encode())


def model_key(workspace_id: int, model: str) -> str:
    return f"generated-response-audio/estimates/{workspace_id}/{model}.json"


def estimate_generation(
    *,
    model: str,
    characters: int,
    started_at: float | None,
    progress: dict[str, Any],
    samples: list[float],
    now: float | None = None,
) -> dict[str, Any]:
    """A remaining-time range, refined by completed chunks, never a false zero."""
    now = time.time() if now is None else now
    elapsed = max(0, now - started_at) if started_at is not None else 0
    completed = min(characters, max(0, int(progress.get("completed_characters", 0))))
    completed_at = float(progress.get("completed_at", started_at or now))
    # Cold-start heuristics only; real successes replace them for this model.
    seconds_per_character = (
        statistics.median(samples) if samples else (1 / 40 if model == "eleven_v4" else 1 / 100)
    )
    basis = "recent generations" if samples else "initial estimate"
    if completed and started_at is not None and completed_at > started_at:
        seconds_per_character = (completed_at - started_at) / completed
        basis = "this generation"
    remaining_work = max(0, characters - completed) * seconds_per_character + 5
    since_completion = max(0, now - completed_at) if started_at is not None else 0
    upper = remaining_work * 1.8 - since_completion
    overdue = started_at is not None and upper <= 0
    return {
        "model": model,
        "narration_characters": characters,
        "completed_characters": completed,
        "completed_chunks": int(progress.get("completed_chunks", 0)),
        "total_chunks": progress.get("total_chunks"),
        "elapsed_seconds": round(elapsed),
        "estimate_basis": basis,
        "remaining_seconds_low": None
        if overdue
        else max(5, round(remaining_work * 0.6 - since_completion)),
        "remaining_seconds_high": None if overdue else max(10, round(upper)),
        "taking_longer": overdue,
    }
