"""Creation-time pins for the user's chat review queue."""

from __future__ import annotations

import time
from collections.abc import Mapping

from omnigent.stores.conversation_store import (
    PINNED_LABEL_KEY,
    SIDE_CHAT_LABEL_KEY,
    pinned_label_key,
)


def initial_session_labels(
    labels: Mapping[str, str] | None,
    *,
    user_id: str | None,
    parent_session_id: str | None = None,
) -> dict[str, str]:
    """Pin a new sidebar chat for its creator in the creation transaction.

    Call only after client-label validation and only for creation. An explicit
    canonical pin value (including an empty opt-out) takes precedence. Child
    sessions and hidden side chats do not enter the review queue automatically.
    Existing sessions, including ones the user has unpinned, are never backfilled.
    """
    result = dict(labels or {})
    if PINNED_LABEL_KEY in result:
        pin = result.pop(PINNED_LABEL_KEY)
    elif parent_session_id is None and result.get(SIDE_CHAT_LABEL_KEY) != "1":
        pin = str(time.time_ns() // 1_000_000)
    else:
        pin = ""
    if pin:
        result[pinned_label_key(user_id)] = pin
    return result
