"""Tests for the generic harness-model-options route (``/v1/harness-model-options``).

Three lanes are exercised end-to-end:

* ``opencode-native`` (OpenCode Free) — reads the local catalog at
  ``~/.cache/homelab/opencode-free-models.json`` and surfaces only
  ``free=true`` entries. Test injects a small catalog.
* ``opencode-native-minimax-token-plan`` (MiniMax Token Plan / subscription) —
  reads the local catalog at
  ``~/.cache/homelab/opencode-minimax-token-plan-models.json`` and
  rejects any id whose provider prefix is not one of the two Token Plan
  prefixes (``minimax-coding-plan`` or ``minimax-cn-coding-plan``).
  API-metered ``minimax/...`` and ``minimax-cn/...`` ids must NEVER
  leak through.
* ``opencode-native-codex-subscription`` (Codex Subscription) — today
  the local catalog is OPTIONAL: the resolver returns ``{"models":
  [], "error": "Codex Subscription catalog not found ..."}`` so the
  picker surfaces the empty / setup state rather than inventing models.
  No OpenAI API key, no OpenAI billing fallback.

The route resolves the catalog paths from ``Path.home()``, so tests
monkeypatch both ``Path.home()`` and the resolver's path constants to
a temp directory.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest

from omnigent.server.routes import harness_model_options as hmo_module


def _write_catalog(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture()
def catalog_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Force catalog reads to a temp directory.

    The shared ``OPENCODE_NATIVE_LANES`` table (which the resolver
    reads from) holds each lane's catalog path. The table is
    constructed at import time with ``Path.home() / ".cache/homelab/<lane>.json"``,
    so the fixture:

    1. Patches ``Path.home()`` to ``tmp_path`` so any code that
       re-reads ``Path.home()`` resolves to the test temp dir.
    2. Re-points every lane's ``catalog_path`` to the same
       ``tmp_path`` so the resolver reads from the test dir. The
       ``OPENCODE_NATIVE_LANES`` tuple is a tuple of frozen
       dataclasses — each lane is replaced with a ``dataclasses.replace``
       that swaps the path. (The dataclass is frozen, so we
       cannot mutate in place.)
    3. Rebuilds the ``_HARNESS_MODEL_PROVIDERS`` registry from the
       patched lanes (the registry is built at import time from
       the same table, so it must be rebuilt after the patch).
    """
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    from omnigent.inner import _opencode_native_lane_config as lane_config_mod
    import dataclasses

    test_cache = tmp_path / ".cache" / "homelab"
    patched_lanes = []
    for lane in lane_config_mod.OPENCODE_NATIVE_LANES:
        new_path = test_cache / lane.catalog_path.name
        patched_lanes.append(dataclasses.replace(lane, catalog_path=new_path))
    monkeypatch.setattr(
        lane_config_mod,
        "OPENCODE_NATIVE_LANES",
        tuple(patched_lanes),
    )
    # Rebuild the resolver-id lookup map too (the resolver module
    # imported its own reference at import time).
    monkeypatch.setattr(
        lane_config_mod,
        "_BY_RESOLVER_ID",
        {lane.resolver_id: lane for lane in patched_lanes},
    )
    # The resolver module's provider registry was built at import
    # time from the original lanes — rebuild it so the patched paths
    # are honoured.
    monkeypatch.setattr(
        hmo_module,
        "_HARNESS_MODEL_PROVIDERS",
        {
            lane.resolver_id: (lambda lane=lane: hmo_module._resolve_lane_models(lane))
            for lane in patched_lanes
        },
    )
    return tmp_path


# ── OpenCode Free lane ────────────────────────────────────────────────


async def test_opencode_native_returns_only_free_models(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """Only entries with ``free: true`` are returned; labels are normalized."""
    _write_catalog(
        catalog_home / ".cache" / "homelab" / "opencode-free-models.json",
        {
            "last_synced_at": "2026-07-03T15:00:00+00:00",
            "models": [
                {
                    "id": "big-pickle",
                    "name": "Big Pickle",
                    "free": True,
                    "context_limit": 128000,
                    "output_limit": 8192,
                },
                {
                    "id": "deepseek-v4-flash-free",
                    "name": "DeepSeek V4 Flash Free",
                    "free": True,
                    "context_limit": 128000,
                    "output_limit": 8192,
                },
                {
                    # NOT free — must be filtered out by the resolver.
                    "id": "anthropic/claude-opus-4",
                    "name": "Anthropic Claude Opus 4",
                    "free": False,
                },
            ],
        },
    )

    resp = await client.get("/v1/harness-model-options?harness=opencode-native")
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "opencode-native"
    assert body["source"] == "opencode-free-catalog"
    # The two free entries come back; the API-billed one does NOT.
    ids = [m["id"] for m in body["models"]]
    assert ids == ["opencode/big-pickle", "opencode/deepseek-v4-flash-free"]
    for m in body["models"]:
        assert m["provider"] == "OpenCode"
        assert m["tier"] == "free"
        assert m["kind"] == "manual-fallback"
        assert m["requires_credentials"] is False
        assert m["billing_risk"] == "none-observed"


async def test_opencode_native_returns_error_when_catalog_missing(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """Missing catalog → empty models + error string; the picker surfaces the state."""
    resp = await client.get("/v1/harness-model-options?harness=opencode-native")
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "opencode-native"
    assert body["models"] == []
    assert "error" in body and body["error"]
    assert "Catalog not found" in body["error"]


# ── MiniMax Token Plan lane ──────────────────────────────────────────


async def test_minimax_token_plan_returns_only_token_plan_prefixes(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """Only Token Plan provider prefixes are admitted; API-metered prefixes are rejected."""
    _write_catalog(
        catalog_home / ".cache" / "homelab" / "opencode-minimax-token-plan-models.json",
        {
            "last_synced_at": "2026-07-03T15:00:00+00:00",
            "models": [
                {
                    "id": "opencode/minimax-coding-plan/MiniMax-M2.7",
                    "provider_id": "minimax-coding-plan",
                    "model_name": "MiniMax-M2.7",
                    "name": "MiniMax M2.7",
                    "region": "international",
                    "context_limit": 204800,
                    "output_limit": 131072,
                    "credentials_present": True,
                },
                {
                    "id": "opencode/minimax-cn-coding-plan/MiniMax-M3",
                    "provider_id": "minimax-cn-coding-plan",
                    "model_name": "MiniMax-M3",
                    "name": "MiniMax M3",
                    "region": "china",
                    "credentials_present": False,
                },
                # Defense-in-depth: even a buggy catalog run that slipped an
                # API-metered id through the sync-script filter must NOT reach
                # the picker.
                {
                    "id": "opencode/minimax/MiniMax-M2.7-api",
                    "provider_id": "minimax",
                    "model_name": "MiniMax-M2.7-api",
                    "name": "MiniMax M2.7 API",
                    "credentials_present": True,
                },
                {
                    "id": "opencode/minimax-cn/MiniMax-M3-api",
                    "provider_id": "minimax-cn",
                    "model_name": "MiniMax-M3-api",
                    "name": "MiniMax M3 API",
                    "credentials_present": True,
                },
            ],
        },
    )

    resp = await client.get(
        "/v1/harness-model-options?harness=opencode-native-minimax-token-plan"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "opencode-native-minimax-token-plan"
    ids = [m["id"] for m in body["models"]]
    # Only the two Token Plan ids are admitted; the API-metered variants are
    # filtered at three layers (sync, verify, resolver) — this pins the
    # resolver layer.
    assert ids == [
        "opencode/minimax-coding-plan/MiniMax-M2.7",
        "opencode/minimax-cn-coding-plan/MiniMax-M3",
    ]
    for m in body["models"]:
        assert m["provider"] == "MiniMax"
        assert m["tier"] == "subscription"
        assert m["kind"] == "token-plan"
        assert m["billing_risk"] == "token-plan-subscription"
        assert m["requires_credentials"] is True
        assert m["manual_fallback_only"] is True
        # credentials_present is BOOLEAN only — never a secret value.
        assert isinstance(m["credentials_present"], bool)
    # The international entry carries its region label.
    m27 = next(m for m in body["models"] if m["id"].endswith("/MiniMax-M2.7"))
    assert "international" in m27["label"]


async def test_minimax_token_plan_returns_error_when_catalog_missing(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """No catalog → empty models + error string; the picker surfaces the state."""
    resp = await client.get(
        "/v1/harness-model-options?harness=opencode-native-minimax-token-plan"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "opencode-native-minimax-token-plan"
    assert body["models"] == []
    assert "error" in body and body["error"]


# ── Codex Subscription lane ──────────────────────────────────────────


async def test_codex_subscription_returns_empty_with_setup_message_when_catalog_missing(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """No local catalog → empty models + setup message. NO OpenAI fallback."""
    resp = await client.get(
        "/v1/harness-model-options?harness=opencode-native-codex-subscription"
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "opencode-native-codex-subscription"
    assert body["models"] == []
    assert "error" in body and body["error"]
    # The error must clearly state this is a local-not-configured state and
    # explicitly disclaim the OpenAI API-billed path — the picker surfaces
    # this verbatim, so a future operator can act on it.
    assert "Codex Subscription catalog not found" in body["error"]
    assert "NEVER falls back to the OpenAI API-billed path" in body["error"]


async def test_codex_subscription_returns_empty_even_with_catalog_until_prefix_verified(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """Even with a local catalog present, the resolver rejects every entry
    until a Codex-subscription provider prefix is verified. The
    allowlist is intentionally empty in this commit (no public OpenCode
    Codex-subscription prefix is known), so the picker surfaces empty +
    setup state rather than inventing models.
    """
    _write_catalog(
        catalog_home / ".cache" / "homelab" / "opencode-codex-subscription-models.json",
        {
            "last_synced_at": "2026-07-03T15:00:00+00:00",
            "models": [
                # An OpenAI API-billed id — must NEVER leak.
                {
                    "id": "opencode/codex/gpt-5.4",
                    "provider_id": "codex",
                    "model_name": "gpt-5.4",
                    "name": "Codex gpt-5.4",
                    "credentials_present": True,
                },
                # A future hypothetical Codex-subscription provider id — also
                # rejected until the allowlist is populated.
                {
                    "id": "opencode/codex-subscription/gpt-5.4",
                    "provider_id": "codex-subscription",
                    "model_name": "gpt-5.4",
                    "name": "Codex Subscription gpt-5.4",
                    "credentials_present": True,
                },
            ],
        },
    )

    resp = await client.get(
        "/v1/harness-model-options?harness=opencode-native-codex-subscription"
    )
    assert resp.status_code == 200
    body = resp.json()
    # Empty — the resolver rejected every entry because the allowlist is
    # intentionally empty. No silent substitution.
    assert body["models"] == []
    # The source is still reported as the catalog path (proves the file was
    # read), so the picker can show "configured but not verified" if needed.
    assert body["source"] == "opencode-codex-subscription-catalog"
    # And the resolver never suggests an OpenAI fallback in the response.
    assert "OpenAI" not in json.dumps(body)


# ── Registry contract ───────────────────────────────────────────────


async def test_harness_model_options_returns_empty_for_unknown_harness(
    client: httpx.AsyncClient,
    catalog_home: Path,
) -> None:
    """An unknown harness id is acknowledged with an empty list and a clear note."""
    resp = await client.get("/v1/harness-model-options?harness=does-not-exist")
    assert resp.status_code == 200
    body = resp.json()
    assert body["harness"] == "does-not-exist"
    assert body["models"] == []
    assert body["source"] is None
    assert "note" in body and "No model provider registered" in body["note"]


def test_registry_lists_all_three_lanes() -> None:
    """The provider registry must expose all three lanes so the picker can route by harness id."""
    assert set(hmo_module._HARNESS_MODEL_PROVIDERS.keys()) == {
        "opencode-native",
        "opencode-native-minimax-token-plan",
        "opencode-native-codex-subscription",
    }


def test_codex_subscription_allowlist_is_intentionally_empty() -> None:
    """Pin the fail-closed state — no public Codex-subscription provider prefix is verified yet.

    The allowlist now lives in the shared
    ``OPENCODE_NATIVE_LANES`` table (single source of truth shared
    with the executor); the resolver and the executor gate on the
    same membership list. This test pins the resolver's view of
    the allowlist.
    """
    from omnigent.inner._opencode_native_lane_config import (
        lane_for_resolver_id,
    )

    lane = lane_for_resolver_id("opencode-native-codex-subscription")
    assert lane is not None
    assert lane.allowed_provider_prefixes == frozenset()


def test_minimax_token_plan_allowlist_lists_only_token_plan_prefixes() -> None:
    """Pin the two Token Plan prefixes (and only those) as the API-metered guard.

    The allowlist now lives in the shared ``OPENCODE_NATIVE_LANES``
    table (single source of truth shared with the executor).
    """
    from omnigent.inner._opencode_native_lane_config import (
        lane_for_resolver_id,
    )

    lane = lane_for_resolver_id("opencode-native-minimax-token-plan")
    assert lane is not None
    assert lane.allowed_provider_prefixes == frozenset(
        {"minimax-coding-plan", "minimax-cn-coding-plan"}
    )


def test_resolver_and_executor_allowlists_agree() -> None:
    """The resolver and the executor must agree on every SUBSCRIPTION
    lane's allowlist — that's the whole point of the shared
    ``OPENCODE_NATIVE_LANES`` table.

    A divergence here means a buggy catalog run could be admitted by
    the picker (because the resolver allows it) but rejected by the
    runner (because the executor disallows it) — or vice versa.
    Either is a silent pick-then-crash bug. This test fails loudly
    on any such divergence.
    """
    from omnigent.inner._opencode_native_lane_config import (
        lane_for_resolver_id,
    )
    from omnigent.inner import (
        opencode_native_minimax_token_plan_harness as minimax_mod,
    )
    from omnigent.inner import (
        opencode_native_codex_subscription_harness as codex_mod,
    )

    # The MiniMax Token Plan lane: resolver + executor agree.
    resolver_lane = lane_for_resolver_id("opencode-native-minimax-token-plan")
    assert resolver_lane is not None
    assert resolver_lane.allowed_provider_prefixes == (
        minimax_mod._MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES
    )
    # The Codex Subscription lane: resolver + executor agree.
    resolver_lane = lane_for_resolver_id("opencode-native-codex-subscription")
    assert resolver_lane is not None
    assert resolver_lane.allowed_provider_prefixes == (
        codex_mod._OPENCODE_CODEX_SUBSCRIPTION_ALLOWED_PROVIDER_PREFIXES
    )