"""Version tokens used by conditional session mutations."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class SessionMutationFingerprint:
    """Mutation state protected by the conditional session-delete fence.

    ``updated_at`` remains the display-order timestamp.  The item-position
    allocator and review-comment fingerprint cover writes that intentionally
    do not change that timestamp.
    """

    next_position: int | None
    comments_count: int
    comments_updated_at: int | None


def session_etag(
    updated_at: int,
    labels: Mapping[str, str],
    *,
    mutation: SessionMutationFingerprint | None = None,
) -> str:
    """Return a stable validator for a session snapshot.

    Labels live in their own table and therefore are not guaranteed to bump
    ``conversations.updated_at``. Include the complete label mapping so a
    pin, retention change, or other label edit cannot race a destructive
    conditional mutation while reusing the same timestamp.  ``mutation``
    additionally covers first-turn item writes and review-comment writes that
    deliberately leave the display-order timestamp unchanged.
    """
    payload_data: dict[str, object] = {
        "updated_at": updated_at,
        "labels": sorted(labels.items()),
    }
    if mutation is not None:
        payload_data["mutation"] = {
            "next_position": mutation.next_position,
            "comments_count": mutation.comments_count,
            "comments_updated_at": mutation.comments_updated_at,
        }
    payload = json.dumps(
        payload_data,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    digest = hashlib.sha256(payload).hexdigest()
    return f'"{updated_at}-{digest}"'
