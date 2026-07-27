"""Unit tests for :mod:`omnigent.server.omniroute_catalog`.

These tests assert the catalog invariants that downstream code relies on:

* The three curated combos (``auto/best-coding``, ``auto/coding:fast``,
  ``auto/coding:reliable``) always appear, with the curated display names
  attached.
* Concrete (non-combo) entries from OmniRoute's ``/v1/models`` are excluded.
* Duplicate ids are deduped (first occurrence wins).
* Curated IDs / slashes survive normalization verbatim.
* Display name lookup falls back to the raw id for unknown combos.
* Live fetch falls through to the cached listing when the endpoint is
  unreachable, and to the curated fallback when both are unavailable.
* ``validate_model_override`` accepts the colon and slash characters used by
  the curated combo ids.

The tests do NOT contact the live OmniRoute endpoint — all live paths are
exercised via httpx transport mocks.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

import httpx
import pytest

from omnigent.model_override import validate_model_override
from omnigent.server.omniroute_catalog import (
    CURATED_COMBO_IDS,
    OmniRouteComboEntry,
    _clear_cache_for_tests,
    _resolve_api_key,
    curated_combo_catalog,
    dedupe_preserve_order,
    fetch_omniroute_combo_catalog,
    omniroute_combo_display_name,
)
from omnigent.server.omniroute_routes import (
    NATIVE_OMNIROUTE_ROUTE_IDS,
    OMNIROUTE_ROUTE_CATALOG,
    is_known_route_id,
)


@pytest.fixture(autouse=True)
def _reset_cache() -> Iterator[None]:
    """Always reset the cache between tests so state leaks cannot cross tests."""
    _clear_cache_for_tests()
    yield
    _clear_cache_for_tests()


# ── Curated invariants ─────────────────────────────────────────────────────


def test_curated_combo_ids_present_and_ordered():
    """The curated catalog must contain exactly the three required combos."""
    assert CURATED_COMBO_IDS == (
        "auto/best-coding",
        "auto/coding:fast",
        "auto/coding:reliable",
    )


def test_curated_combos_have_friendly_display_names():
    """Curated combos get curated display names; raw ids are preserved verbatim."""
    catalog = curated_combo_catalog()
    assert len(catalog) == 3
    by_id = {entry.id: entry for entry in catalog}
    assert by_id["auto/best-coding"].display_name == "OmniRoute Coding Best"
    assert by_id["auto/coding:fast"].display_name == "OmniRoute Coding Fast"
    assert by_id["auto/coding:reliable"].display_name == "OmniRoute Coding Reliable"
    # IDs are preserved verbatim — slashes, colons, dots survive intact.
    assert by_id["auto/best-coding"].id == "auto/best-coding"
    assert by_id["auto/coding:fast"].id == "auto/coding:fast"
    assert by_id["auto/coding:reliable"].id == "auto/coding:reliable"


def test_curated_combos_are_provider_omniroute_kind_combo():
    """Each entry must report provider=omniroute, kind=combo so the picker
    can distinguish a curated combo from a concrete model."""
    for entry in curated_combo_catalog():
        assert entry.provider == "omniroute"
        assert entry.kind == "combo"


def test_curated_combo_ids_are_validated_as_native_routes():
    """The native catalog must accept all three curated combo ids verbatim."""
    for combo_id in CURATED_COMBO_IDS:
        assert is_known_route_id(combo_id)
        assert combo_id in NATIVE_OMNIROUTE_ROUTE_IDS
        assert combo_id in OMNIROUTE_ROUTE_CATALOG


def test_validate_model_override_accepts_combo_ids():
    """The model-override validator must accept slashes + colons verbatim."""
    for combo in CURATED_COMBO_IDS:
        # Should NOT raise — these ids hit the live dispatch path.
        assert validate_model_override(combo) == combo


def test_validate_model_override_preserves_bracket_suffix_shape():
    """Bracket suffixes (sometimes used by gateway vendors) must also survive."""
    assert validate_model_override("openai/gpt-4o[1m]") == "openai/gpt-4o[1m]"


# ── Wire serialization ────────────────────────────────────────────────────


def test_to_wire_is_json_safe_with_camel_case_keys():
    """Wire payload matches the contract the SPA reads:
    * ``display_name`` (snake_case as stored on disk; mirror of native schema).
    * ``reasoning_efforts`` is a list (not tuple)."""
    catalog = curated_combo_catalog()
    first = catalog[0]
    wire = first.to_wire()
    json.dumps(wire)  # must not raise
    assert wire["id"] == first.id
    assert wire["display_name"] == first.display_name
    assert wire["provider"] == "omniroute"
    assert wire["kind"] == "combo"
    assert isinstance(wire["reasoning_efforts"], list)


def test_curated_combo_effort_ranges_match_native_profile():
    """Each curated combo carries the same allowed-effort range the validator
    enforces — so the picker can't offer an effort the runtime would reject."""
    by_id = {entry.id: entry for entry in curated_combo_catalog()}
    for combo_id, entry in by_id.items():
        profile_efforts = OMNIROUTE_ROUTE_CATALOG[combo_id].allowed_reasoning_efforts
        assert tuple(entry.reasoning_efforts) == profile_efforts


# ── Deduplication ──────────────────────────────────────────────────────────


def test_dedupe_preserve_order_keeps_first_occurrence():
    """Duplicate ids collapse to the first occurrence so the curated display
    name wins over a later duplicate with a generic name."""
    entries = [
        OmniRouteComboEntry(
            id="auto/best-coding",
            display_name="Canonical",
            provider="omniroute",
            kind="combo",
            reasoning_efforts=("medium", "high"),
            max_reasoning_effort="high",
            default_reasoning_effort="medium",
            requires_explicit_approval=False,
        ),
        OmniRouteComboEntry(
            id="auto/best-coding",
            display_name="Duplicate",
            provider="omniroute",
            kind="combo",
            reasoning_efforts=("low",),
            max_reasoning_effort="low",
            default_reasoning_effort="low",
            requires_explicit_approval=False,
        ),
        OmniRouteComboEntry(
            id="auto/coding:fast",
            display_name="Fast",
            provider="omniroute",
            kind="combo",
            reasoning_efforts=("low", "medium"),
            max_reasoning_effort="medium",
            default_reasoning_effort="low",
            requires_explicit_approval=False,
        ),
    ]
    deduped = dedupe_preserve_order(entries)
    assert [e.id for e in deduped] == ["auto/best-coding", "auto/coding:fast"]
    # First-occurrence wins for display name.
    assert deduped[0].display_name == "Canonical"


# ── Live fetch (mocked transport) ──────────────────────────────────────────


class _StaticTransport(httpx.AsyncBaseTransport):
    """httpx transport mock that returns a fixed JSON body."""

    def __init__(self, body: dict[str, Any], status_code: int = 200) -> None:
        self._body = body
        self._status_code = status_code

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            self._status_code,
            json=self._body,
            request=request,
        )


def _live_payload(*, extra_ids: tuple[str, ...] = ()) -> dict[str, Any]:
    """Build a realistic OmniRoute ``/v1/models`` payload — three curated
    combos plus a few extras; never includes non-combo entries."""
    curated_ids = list(CURATED_COMBO_IDS) + list(extra_ids)
    return {
        "object": "list",
        "data": [
            {
                "id": combo_id,
                "object": "model",
                "owned_by": "combo",
                "context_length": 1048576,
                "max_input_tokens": 1048576,
                "max_output_tokens": 512000,
                "capabilities": {
                    "tool_calling": True,
                    "reasoning": True,
                    "thinking": True,
                    "temperature": True,
                },
            }
            for combo_id in curated_ids
        ],
    }


class _FailingTransport(httpx.AsyncBaseTransport):
    """Transport that always raises an httpx error (simulates OmniRoute down)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated offline", request=request)


def test_router_api_key_alias_is_used_for_live_catalog(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OMNIGENT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("OMNIGENT_ROUTER_API_KEY", "router-key")

    assert _resolve_api_key(None) == "router-key"


def test_live_fetch_returns_curated_three_with_friendly_names():
    """Live fetch applies curated display names to the required combos."""
    transport = _StaticTransport(_live_payload())
    combos, source = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://test.localhost/v1", api_key="test-key", transport=transport
        )
    )
    assert source == "live"
    assert len(combos) == 3
    by_id = {entry.id: entry for entry in combos}
    assert by_id["auto/best-coding"].display_name == "OmniRoute Coding Best"
    assert by_id["auto/coding:fast"].display_name == "OmniRoute Coding Fast"
    assert by_id["auto/coding:reliable"].display_name == "OmniRoute Coding Reliable"


def test_live_fetch_preserves_extra_combos_unknown_to_native_catalog():
    """A combo id not in the static catalog still gets a picker row, with
    sensible defaults (the curated display-name map wins when present)."""
    transport = _StaticTransport(_live_payload(extra_ids=("auto/some-future",)))
    combos, _ = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://test.localhost/v1", api_key="test-key", transport=transport
        )
    )
    by_id = {entry.id: entry for entry in combos}
    assert "auto/some-future" in by_id
    # Unknown id → display name == id verbatim (never a hash, never an empty string).
    assert by_id["auto/some-future"].display_name == "auto/some-future"


def test_live_fetch_excludes_non_combo_entries():
    """Concrete (non-combo) entries must NOT be served as combos."""
    payload = {
        "data": [
            {
                "id": "auto/best-coding",
                "object": "model",
                "owned_by": "combo",
            },
            {
                "id": "concrete/model-x",
                "object": "model",
                "owned_by": "anthropic",  # NOT a combo
            },
            {
                "id": "auto/best-coding",  # duplicate id; should dedupe
                "object": "model",
                "owned_by": "combo",
            },
        ]
    }
    transport = _StaticTransport(payload)
    combos, source = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://test.localhost/v1", api_key="k", transport=transport
        )
    )
    assert source == "live"
    # Non-combo dropped; duplicate deduped; curated combo preserved.
    ids = [entry.id for entry in combos]
    assert ids == ["auto/best-coding"]


def test_live_fetch_malformed_payload_falls_back_to_curated():
    """A 200 with malformed JSON payload is treated as a fetch failure."""
    transport = _StaticTransport({"data": "not a list"})
    combos, source = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://test.localhost/v1", api_key="k", transport=transport
        )
    )
    assert source == "fallback_curated"
    assert {c.id for c in combos} == set(CURATED_COMBO_IDS)


def test_unreachable_endpoint_with_empty_cache_falls_back_to_curated():
    """Live fetch fails + no cache → curated fallback."""
    transport = _FailingTransport()
    combos, source = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://offline.test/v1", api_key="k", transport=transport
        )
    )
    assert source == "fallback_curated"
    assert {c.id for c in combos} == set(CURATED_COMBO_IDS)


def test_unreachable_endpoint_with_cache_returns_cached():
    """Live fetch fails after a successful first fetch → cached listing."""
    good = _StaticTransport(_live_payload())
    cached, _ = asyncio.run(
        fetch_omniroute_combo_catalog(base_url="http://flaky.test/v1", api_key="k", transport=good)
    )
    assert len(cached) == 3

    # Now the endpoint goes offline — cache must serve the previous listing.
    bad = _FailingTransport()
    combos, source = asyncio.run(
        fetch_omniroute_combo_catalog(base_url="http://flaky.test/v1", api_key="k", transport=bad)
    )
    assert source == "cache"
    assert {c.id for c in combos} == set(CURATED_COMBO_IDS)


def test_cache_keys_distinct_credentials():
    """Different API keys against the same URL must not share cache entries."""
    transport = _StaticTransport(_live_payload())
    combos_a, _ = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://tenant.test/v1", api_key="tenant-a", transport=transport
        )
    )
    combos_b, _ = asyncio.run(
        fetch_omniroute_combo_catalog(
            base_url="http://tenant.test/v1", api_key="tenant-b", transport=transport
        )
    )
    # Both served live; the cache key isolates them so a tenant-A outage
    # never serves tenant-B's listing.
    assert len(combos_a) == 3
    assert len(combos_b) == 3


def test_omniroute_combo_display_name_lookup():
    """Public display-name lookup: curated wins, native profile wins, else id."""
    assert omniroute_combo_display_name("auto/best-coding") == "OmniRoute Coding Best"
    # Unknown combo falls back to the raw id — preserves slashes/colons.
    assert omniroute_combo_display_name("auto/some-future") == "auto/some-future"
    # Native catalog entry (no curated label) uses the profile's display name.
    assert omniroute_combo_display_name("auto/coding") == "OmniRoute Coding"
