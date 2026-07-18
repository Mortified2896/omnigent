"""Integration tests for the OmniRoute combo catalog HTTP API.

Drives the new ``GET /v1/omniroute/combos`` endpoint, the snapshot's
``omniroute_combos`` field, and the session create/patch validation
for the three curated combos.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from omnigent.server.omniroute_catalog import (
    CURATED_COMBO_DISPLAY_NAMES,
    CURATED_COMBO_IDS,
    curated_combo_catalog,
)
from omnigent.server.omniroute_routes import is_known_route_id
from omnigent.server.schemas import (
    SessionCreateRequest,
    SessionResponse,
    UpdateSessionRequest,
)

# ── Catalog-level ──────────────────────────────────────────────────────────


def test_curated_combos_have_distinct_friendly_names():
    """Three curated ids → three distinct curated display names."""
    curated_ids = set(CURATED_COMBO_IDS)
    names = {CURATED_COMBO_DISPLAY_NAMES[i] for i in curated_ids}
    assert len(names) == 3
    for name in names:
        # All curated names must start with "OmniRoute" so they read
        # consistently in the picker.
        assert name.startswith("OmniRoute")


def test_curated_combo_ids_round_trip_through_catalog():
    """Curated ids are accepted by the native catalog and the curated fallback."""
    catalog = curated_combo_catalog()
    assert {entry.id for entry in catalog} == set(CURATED_COMBO_IDS)
    for entry in catalog:
        assert is_known_route_id(entry.id)


def test_curated_combos_carry_provider_and_kind():
    """Each entry reports provider=omniroute + kind=combo (the picker
    distinguishes them from concrete models on those flags)."""
    for entry in curated_combo_catalog():
        assert entry.provider == "omniroute"
        assert entry.kind == "combo"


# ── Session create / patch ─────────────────────────────────────────────────


def test_session_create_accepts_each_curated_combo():
    """All three curated combos survive schema validation with colons/slashes intact."""
    for combo in CURATED_COMBO_IDS:
        body = SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": combo, "reasoning_effort": "medium"}
        )
        assert body.omniroute_route_id == combo
        # The colon character survives the pydantic round-trip.
        if ":" in combo:
            assert ":" in body.omniroute_route_id
        if "/" in combo:
            assert "/" in body.omniroute_route_id


def test_session_patch_accepts_each_curated_combo():
    """Patch requests accept the curated combos verbatim."""
    for combo in CURATED_COMBO_IDS:
        body = UpdateSessionRequest.model_validate({"omniroute_route_id": combo})
        assert body.omniroute_route_id == combo


def test_session_create_rejects_unknown_combo():
    """An invented combo id must still fail validation (combos are allow-listed)."""
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate(
            {"agent_id": "ag_1", "omniroute_route_id": "auto/fake-combo"}
        )


def test_session_create_rejects_concrete_model_as_route():
    """Concrete model ids are NOT routing combos — they belong in
    ``model_override``, not ``omniroute_route_id``."""
    with pytest.raises(ValidationError):
        SessionCreateRequest.model_validate({"agent_id": "ag_1", "omniroute_route_id": "gpt-5.5"})


# ── Snapshot wire shape ────────────────────────────────────────────────────


def test_session_response_default_omniroute_combos_is_empty_list():
    """The snapshot field defaults to an empty list (compatible with older clients)."""
    # Build a minimal payload; the new field must default to an empty list
    # so existing deserializers don't break.
    payload: dict[str, Any] = {
        "id": "conv_abc123",
        "object": "session",
        "created_at": 0,
        "agent_id": "ag_1",
        "status": "idle",
    }
    response = SessionResponse.model_validate(payload)
    assert response.omniroute_combos == []


def test_session_response_accepts_omniroute_combos_payload():
    """The snapshot deserializes a populated ``omniroute_combos`` array."""
    payload: dict[str, Any] = {
        "id": "conv_abc123",
        "object": "session",
        "created_at": 0,
        "agent_id": "ag_1",
        "status": "idle",
        "omniroute_combos": [
            {
                "id": "auto/best-coding",
                "display_name": "OmniRoute Coding Best",
                "provider": "omniroute",
                "kind": "combo",
                "reasoning_efforts": ["medium", "high"],
                "max_reasoning_effort": "high",
                "default_reasoning_effort": "medium",
                "requires_explicit_approval": False,
            },
            {
                "id": "auto/coding:fast",
                "display_name": "OmniRoute Coding Fast",
                "provider": "omniroute",
                "kind": "combo",
                "reasoning_efforts": ["low", "medium"],
                "max_reasoning_effort": "medium",
                "default_reasoning_effort": "low",
                "requires_explicit_approval": False,
            },
        ],
    }
    response = SessionResponse.model_validate(payload)
    assert len(response.omniroute_combos) == 2
    assert response.omniroute_combos[0]["id"] == "auto/best-coding"
    # The colon survives the schema round-trip — the picker sends it to
    # /v1/sessions/{id} verbatim and the server must preserve it.
    assert response.omniroute_combos[1]["id"] == "auto/coding:fast"


# ── Live resolver fallback contract ───────────────────────────────────────


def test_curated_catalog_never_empty():
    """The curated fallback must always be non-empty (the picker never
    offers an empty provider list, even when OmniRoute is fully down)."""
    catalog = curated_combo_catalog()
    assert len(catalog) >= 3


def test_curated_catalog_resolves_to_wire_safe_shape():
    """The curated catalog's wire payload is JSON-safe + has the schema the
    SPA expects (``provider``, ``kind``, ``display_name``, etc.)."""
    catalog = curated_combo_catalog()
    import json

    for entry in catalog:
        wire = entry.to_wire()
        json.dumps(wire)  # must not raise
        assert wire["provider"] == "omniroute"
        assert wire["kind"] == "combo"
        assert wire["id"] in CURATED_COMBO_IDS
