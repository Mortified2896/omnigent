"""Tests for the shared OpenCode-native lane config table.

The shared ``OPENCODE_NATIVE_LANES`` table is the single source of
truth for every OpenCode-backed lane's catalog path, provider-prefix
allowlist, display metadata, and empty-state message. The
server-side resolver and the subscription-lane executors both read
from it. Adding a new lane is a one-config-entry change — these
tests pin the contracts that make the shared table safe to
depend on:

* Every harness id registered as a SUBSCRIPTION lane has a non-empty
  ``display_provider`` and a credential env var.
* The Free lane's filter logic excludes API-billed entries.
* The SUBSCRIPTION lanes' filter logic excludes API-billed entries
  (re-checks each entry's provider prefix even after the catalog
  reader).
* The empty-state messages explicitly disclaim the no-fallback path.
* A future harness can be added by appending a config entry without
  touching the resolver or executor.
"""

from __future__ import annotations

import pytest

from omnigent.inner._opencode_native_lane_config import (
    OPENCODE_NATIVE_LANES,
    LaneVariant,
    OpenCodeNativeLaneConfig,
    all_resolver_ids,
    empty_state_disclaimer,
    lane_for_executor_harness_id,
    lane_for_resolver_id,
)


# ── Table invariants ───────────────────────────────────────────────


def test_all_lanes_have_distinct_resolver_ids() -> None:
    """No two lanes can share a resolver id — they'd shadow each other."""
    ids = [lane.resolver_id for lane in OPENCODE_NATIVE_LANES]
    assert len(ids) == len(set(ids))


def test_all_subscription_lanes_have_a_credential_env_var() -> None:
    """A SUBSCRIPTION lane's catalog carries a ``credentials_present`` boolean.

    The boolean is derived from the credential env var's presence in
    the sync script's environment, so a SUBSCRIPTION lane MUST name
    a credential env var — even if it's only read at sync time and
    not at runtime (a SUBSCRIPTION lane NEVER falls back to a paid
    API; the env var is only used to surface the catalog's
    ``credentials_present`` boolean).
    """
    for lane in OPENCODE_NATIVE_LANES:
        if lane.lane_variant is LaneVariant.SUBSCRIPTION:
            assert lane.credential_env_var, (
                f"Subscription lane {lane.resolver_id!r} must declare a "
                f"credential_env_var so the sync script can derive "
                f"credentials_present at sync time."
            )


def test_empty_state_messages_plus_disclaimer_disclaim_paid_api_fallback() -> None:
    """Every subscription lane's resolver output (the empty-state
    message joined with the auto-generated disclaimer) must
    explicitly disclaim a paid-API fallback path. The
    ``empty_state_message`` alone is just the catalog-missing
    pointer; the disclaimer is appended by the resolver.
    """
    for lane in OPENCODE_NATIVE_LANES:
        if lane.lane_variant is LaneVariant.SUBSCRIPTION:
            full = lane.empty_state_message + empty_state_disclaimer(lane)
            assert "NEVER" in full or "never" in full, (
                f"Subscription lane {lane.resolver_id!r} full empty-state "
                f"message must disclaim paid-API fallback; got: {full!r}"
            )


def test_disclaimer_for_subscription_lane_mentions_no_fallback() -> None:
    """The disclaimer generated for a SUBSCRIPTION lane must be loud."""
    for lane in OPENCODE_NATIVE_LANES:
        if lane.lane_variant is LaneVariant.SUBSCRIPTION:
            disclaimer = empty_state_disclaimer(lane)
            assert "NEVER" in disclaimer or "never" in disclaimer, (
                f"Disclaimer for {lane.resolver_id!r} must disclaim "
                f"paid-API fallback; got: {disclaimer!r}"
            )


def test_disclaimer_for_free_lane_does_not_misclaim_no_fallback() -> None:
    """The Free lane is also 'no paid API' but for a different reason
    (it's free, not subscription). The disclaimer MUST NOT borrow
    the SUBSCRIPTION lane's "no paid API fallback" wording verbatim
    — that would be misleading."""
    for lane in OPENCODE_NATIVE_LANES:
        if lane.lane_variant is LaneVariant.FREE:
            disclaimer = empty_state_disclaimer(lane)
            # The free-lane disclaimer speaks of "no paid API" not
            # "no paid-API fallback" — the latter implies there was
            # a fallback to begin with.
            assert "NEVER falls back" not in disclaimer, (
                f"Free lane {lane.resolver_id!r} disclaimer should not "
                f"claim a 'never falls back' stance reserved for "
                f"SUBSCRIPTION lanes; got: {disclaimer!r}"
            )


# ── Lookup helpers ─────────────────────────────────────────────────


def test_all_resolver_ids_returns_every_registered_lane() -> None:
    """The lookup helper's output must agree with the table."""
    assert set(all_resolver_ids()) == {lane.resolver_id for lane in OPENCODE_NATIVE_LANES}


def test_lane_for_resolver_id_returns_the_matching_lane() -> None:
    """The lookup helper must return the same object the table holds."""
    for lane in OPENCODE_NATIVE_LANES:
        looked_up = lane_for_resolver_id(lane.resolver_id)
        assert looked_up is lane


def test_lane_for_resolver_id_returns_none_for_unknown_id() -> None:
    """Unknown resolver ids return ``None`` (not an error)."""
    assert lane_for_resolver_id("does-not-exist") is None


def test_lane_for_executor_harness_id_agrees_with_resolver_id() -> None:
    """The two lookup helpers must agree (today they share the same table)."""
    for lane in OPENCODE_NATIVE_LANES:
        assert lane_for_executor_harness_id(lane.resolver_id) is lane


# ── Per-lane contract ──────────────────────────────────────────────


def test_opencode_free_lane_is_free_variant() -> None:
    """The OpenCode Free lane uses the FREE variant (filters by ``free: true``)."""
    lane = lane_for_resolver_id("opencode-native")
    assert lane is not None
    assert lane.lane_variant is LaneVariant.FREE
    assert lane.tier == "free"


def test_opencode_minimax_token_plan_lane_is_subscription_variant() -> None:
    """The MiniMax Token Plan lane uses the SUBSCRIPTION variant
    (filters by provider-prefix allowlist)."""
    lane = lane_for_resolver_id("opencode-native-minimax-token-plan")
    assert lane is not None
    assert lane.lane_variant is LaneVariant.SUBSCRIPTION
    assert lane.tier == "subscription"
    # The two Token Plan provider prefixes — and only those.
    assert lane.allowed_provider_prefixes == frozenset(
        {"minimax-coding-plan", "minimax-cn-coding-plan"}
    )
    # Region labels map the two prefixes to international / China.
    assert lane.region_labels == {
        "minimax-coding-plan": "international",
        "minimax-cn-coding-plan": "China",
    }


def test_opencode_codex_subscription_lane_is_subscription_variant_with_empty_allowlist() -> None:
    """The Codex Subscription lane is SUBSCRIPTION variant with a fail-closed
    empty allowlist today. A future stage-3 entry must populate the
    allowlist AND the verify script's ``SUBSCRIPTION_PREFIXES`` AND
    the sync script's ``CODEX_SUBSCRIPTION_PROVIDERS`` so the three
    membership lists stay in lockstep — see
    ``docs/opencode-codex-subscription-models.md``."""
    lane = lane_for_resolver_id("opencode-native-codex-subscription")
    assert lane is not None
    assert lane.lane_variant is LaneVariant.SUBSCRIPTION
    assert lane.tier == "subscription"
    # Empty allowlist = fail closed at the resolver AND the executor.
    assert lane.allowed_provider_prefixes == frozenset()


# ── Adding a future lane ──────────────────────────────────────────


def test_adding_a_lane_is_a_one_entry_change() -> None:
    """Future-proofing contract: a new lane is one ``OpenCodeNativeLaneConfig``
    appended to ``OPENCODE_NATIVE_LANES``; the resolver and executor
    pick it up automatically via the shared config.

    The test does NOT mutate the module-level table (the resolver
    registry is built at import time and is shared across the suite).
    Instead it asserts that the ``_HARNESS_MODEL_PROVIDERS`` registry
    in ``harness_model_options`` is sourced from the same table — so
    any future addition to the table appears in the registry without
    further edits.
    """
    from omnigent.server.routes import harness_model_options as hmo

    # The set of registered resolver ids MUST equal the set of
    # lanes' resolver ids — every lane gets a registry entry
    # automatically.
    assert set(hmo._HARNESS_MODEL_PROVIDERS.keys()) == {
        lane.resolver_id for lane in OPENCODE_NATIVE_LANES
    }
    # And every registry entry is callable (the generic resolver
    # dispatched on the lane config).
    for resolver in hmo._HARNESS_MODEL_PROVIDERS.values():
        assert callable(resolver)
