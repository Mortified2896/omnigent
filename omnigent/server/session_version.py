"""Version tokens used by conditional session mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping


def session_etag(updated_at: int, labels: Mapping[str, str]) -> str:
    """Return a stable validator for a session snapshot.

    Labels live in their own table and therefore are not guaranteed to bump
    ``conversations.updated_at``. Include the complete label mapping so a
    pin, retention change, or other label edit cannot race a destructive
    conditional mutation while reusing the same timestamp.
    """
    payload = json.dumps(
        {"updated_at": updated_at, "labels": sorted(labels.items())},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f'"{updated_at}-{digest}"'
