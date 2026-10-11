"""Safe serialization for the server-owned model-advisor runner binding.

Conversation labels are persisted in ``VARCHAR(256)`` values. A qualified
transport plan or route is larger, so store long JSON values as a short marker
plus bounded chunks. The runner joins and validates those chunks before native
harness startup. Plain JSON labels remain readable for existing sessions.
"""

from __future__ import annotations

from collections.abc import Mapping

_LABEL_VALUE_MAX_LENGTH = 256
_CHUNK_SIZE = 200
_MAX_BINDING_LENGTH = 12_800
_MAX_CHUNKS = 64
_MARKER_PREFIX = "__omnigent_json_chunks_v1__:"
_CHUNK_SUFFIX = ".__chunk__."
_ADVISOR_PREFIX = "omnigent.advisor."
_PREVIOUS_ADVISOR_PREFIX = "omnigent.advisor.previous."


def archive_advisor_binding_labels(labels: Mapping[str, str]) -> tuple[dict[str, str], list[str]]:
    """Retain the previous decision while releasing its active launch binding.

    A manual choice in an already-started chat is a new execution configuration,
    not a rewrite of the confirmed advisor round. Move the server-owned labels,
    including any JSON chunks, together with the session-field update. Native
    runners then resume using the manual choice on the same access lane.
    Response-scoped round bindings and the immutable round record stay intact.
    Retain the round identity so existing native runners keep exact catalog
    validation for the manual pick, including Max, instead of applying SDK
    effort aliases or silently falling back to a different model.
    """
    from omnigent.stores.conversation_store import (
        ADVISOR_MANUAL_CHOICE_LABEL_KEY,
        ADVISOR_ROUND_LABEL_KEY,
    )

    active = {
        key: value
        for key, value in labels.items()
        if key.startswith(_ADVISOR_PREFIX) and not key.startswith(_PREVIOUS_ADVISOR_PREFIX)
    }
    archived = {
        _PREVIOUS_ADVISOR_PREFIX + key.removeprefix(_ADVISOR_PREFIX): value
        for key, value in active.items()
    }
    archived[ADVISOR_MANUAL_CHOICE_LABEL_KEY] = "1"
    return archived, [key for key in active if key != ADVISOR_ROUND_LABEL_KEY]


def encode_transport_binding_label(label_key: str, payload: str) -> dict[str, str]:
    """Return labels that preserve one JSON payload within the label value cap."""
    if not isinstance(label_key, str) or not label_key:
        raise ValueError("transport binding label key must be non-empty")
    if not isinstance(payload, str):
        raise ValueError("transport binding payload must be text")
    if len(payload) <= _LABEL_VALUE_MAX_LENGTH:
        return {label_key: payload}
    if len(payload) > _MAX_BINDING_LENGTH:
        raise ValueError("transport binding payload exceeds the supported size")

    chunks = [
        payload[offset : offset + _CHUNK_SIZE] for offset in range(0, len(payload), _CHUNK_SIZE)
    ]
    if len(chunks) > _MAX_CHUNKS:
        raise ValueError("transport binding payload has too many chunks")
    labels = {label_key: f"{_MARKER_PREFIX}{len(chunks)}"}
    labels.update(
        {f"{label_key}{_CHUNK_SUFFIX}{index}": chunk for index, chunk in enumerate(chunks)}
    )
    if any(len(value) > _LABEL_VALUE_MAX_LENGTH for value in labels.values()):
        raise ValueError("transport binding chunk exceeds the label value limit")
    return labels


def decode_transport_binding_label(labels: Mapping[str, object], label_key: str) -> object:
    """Reassemble a chunked server binding while accepting legacy plain JSON."""
    value = labels.get(label_key)
    if not isinstance(value, str) or not value.startswith(_MARKER_PREFIX):
        return value

    count_text = value[len(_MARKER_PREFIX) :]
    if not count_text.isdecimal():
        raise ValueError("transport binding chunk marker is invalid")
    count = int(count_text)
    if count < 1 or count > _MAX_CHUNKS:
        raise ValueError("transport binding chunk count is invalid")

    chunks: list[str] = []
    for index in range(count):
        chunk = labels.get(f"{label_key}{_CHUNK_SUFFIX}{index}")
        if not isinstance(chunk, str) or len(chunk) > _LABEL_VALUE_MAX_LENGTH:
            raise ValueError("transport binding chunk is missing or invalid")
        chunks.append(chunk)
    payload = "".join(chunks)
    if len(payload) > _MAX_BINDING_LENGTH:
        raise ValueError("transport binding payload exceeds the supported size")
    return payload
