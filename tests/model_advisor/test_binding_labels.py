from __future__ import annotations

import pytest

from omnigent.model_advisor_binding import (
    decode_transport_binding_label,
    encode_transport_binding_label,
)


def test_transport_binding_json_is_chunked_below_the_session_label_limit() -> None:
    label_key = "omnigent.advisor.transport_plan"
    payload = '{"primary":{"route":"' + ("route-data-" * 40) + '"}}'

    labels = encode_transport_binding_label(label_key, payload)

    assert labels[label_key].startswith("__omnigent_json_chunks_v1__:")
    assert max(map(len, labels.values())) <= 256
    assert decode_transport_binding_label(labels, label_key) == payload


def test_transport_binding_decoder_keeps_legacy_plain_json() -> None:
    label_key = "omnigent.advisor.dispatch_route"
    payload = '{"choice":{"model_id":"gpt-6-luna"}}'

    assert decode_transport_binding_label({label_key: payload}, label_key) == payload


def test_transport_binding_decoder_fails_closed_on_missing_chunk() -> None:
    label_key = "omnigent.advisor.dispatch_route"
    labels = encode_transport_binding_label(label_key, '{"route":"' + ("x" * 500) + '"}')
    labels.pop(f"{label_key}.__chunk__.1")

    with pytest.raises(ValueError, match="chunk is missing"):
        decode_transport_binding_label(labels, label_key)


def test_transport_binding_decoder_rejects_malformed_marker() -> None:
    label_key = "omnigent.advisor.dispatch_route"

    with pytest.raises(ValueError, match="marker is invalid"):
        decode_transport_binding_label(
            {label_key: "__omnigent_json_chunks_v1__:not-a-count"}, label_key
        )
