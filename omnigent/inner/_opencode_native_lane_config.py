"""Shared configuration for the OpenCode-native subscription / free lanes.

The OpenCode Native harness exposes several "lanes" through the
generic ``/v1/harness-model-options?harness=<canonical-harness>``
endpoint — today three:

* ``opencode-native`` — the free lane (``opencode/*`` free models).
* ``opencode-native-minimax-token-plan`` — the MiniMax Token Plan
  subscription lane (``minimax-coding-plan/`` and
  ``minimax-cn-coding-plan/``).
* ``opencode-native-codex-subscription`` — the Codex Subscription
  lane (fail-closed today, no public verified provider prefix yet).

Each lane has its own:

* Catalog file under ``~/.cache/homelab/opencode-<lane>-models.json``.
* Provider-prefix allowlist (subscription lanes only; the free lane
  has none — any ``opencode/*`` id admitted).
* Display metadata (provider label, tier, kind, billing risk, etc.).
* Empty-state message (what the picker shows when the catalog is
  missing or empty).
* Credential env var the sync script reads at sync time (subscription
  lanes only).

This module is the **single source of truth** for that config. The
server-side resolver
(``omnigent.server.routes.harness_model_options``) and the
subscription-lane executors (the two
``omnigent.inner.opencode_native_*_harness`` modules) both import
from this table — so the picker, the catalog reader, and the
executor stay in lockstep automatically. Adding a future
OpenCode-backed subscription lane (e.g. for a hypothetical "Claude
Code subscription" or "Pi Pro subscription") is a one-config-entry
change: add a new :class:`SubscriptionLaneConfig` to
``OPENCODE_NATIVE_LANES`` and the resolver / executor / picker
hook up automatically.

Future harnesses (Claude Code native, Codex native, Pi native,
…) can extend the same pattern by adding their own lane configs
keyed by a non-``opencode-native-*`` resolver id — the generic
endpoint already accepts any harness id; the shared config is the
missing piece that lets a future harness plug in a subscription
lane without re-implementing the catalog-read / filter / normalize
boilerplate.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping


class LaneVariant(enum.Enum):
    """Discriminator for the lane kind — drives filter + normalize logic.

    * ``FREE`` — the OpenCode Free lane. Filters by ``m.get("free")
      is True`` in the catalog; no provider-prefix allowlist at the
      executor layer (the catalog is authoritative).
    * ``SUBSCRIPTION`` — a subscription-backed lane. Filters by
      provider-prefix allowlist (``allowed_provider_prefixes``) at
      three layers (sync / verify / resolver); the executor also
      re-checks the allowlist at pin time so a stale stored pick
      can never reach the runner.
    """

    FREE = "free"
    SUBSCRIPTION = "subscription"


@dataclass(frozen=True)
class OpenCodeNativeLaneConfig:
    """One row of the shared OpenCode-native lane config.

    The dataclass is intentionally explicit: every field is read by
    either the resolver, the executor, the sync/verify script
    contract, or the picker / docs. Adding a new field requires
    updating every consumer; merging two consumers' responsibilities
    onto a single field invites silent drift, so they stay
    separate.

    :param resolver_id: Canonical harness id the picker / route uses
        (e.g. ``"opencode-native-minimax-token-plan"``). MUST match
        the harness id the executor exposes AND the key in
        ``_HARNESS_MODEL_PROVIDERS`` in
        ``omnigent.server.routes.harness_model_options``.
    :param catalog_path: Where the local catalog lives
        (``~/.cache/homelab/opencode-<lane>-models.json``). The
        resolver reads from here; the sync script writes to here;
        the verify script reads from here.
    :param display_provider: Human-readable provider name the
        picker shows, e.g. ``"OpenCode"`` / ``"MiniMax"`` /
        ``"Codex"``. NOT the model family — that's separate.
    :param tier: ``"free"`` or ``"subscription"``. Drives the
        access-path grouping in the picker.
    :param kind: Picker ``kind`` field, e.g. ``"manual-fallback"`` /
        ``"token-plan"`` / ``"subscription"``. Matches the
        ``HarnessModelOption.kind`` shape.
    :param billing_risk: Picker ``billing_risk`` field, e.g.
        ``"none-observed"`` / ``"token-plan-subscription"`` /
        ``"subscription"``. Matches the
        ``HarnessModelOption.billing_risk`` shape.
    :param empty_state_message: What the picker shows when the
        catalog is missing. The error from the resolver is a
        longer version of this that includes the harness id and
        the no-paid-fallback disclaimer.
    :param lane_variant: ``LaneVariant.FREE`` or
        ``LaneVariant.SUBSCRIPTION``. Drives which filter the
        resolver / executor apply.
    :param source_label: Stable label the resolver returns as
        ``source`` in the response. Defaults to
        ``f"{resolver_id}-catalog"`` when ``None``. Kept as a
        separate field so legacy / stable strings (e.g. the
        OpenCode Free lane's ``"opencode-free-catalog"``) survive
        the refactor without breaking the picker's source
        handling.
    :param allowed_provider_prefixes: SUBSCRIPTION lanes only.
        Set of provider prefixes (``minimax-coding-plan``,
        ``minimax-cn-coding-plan``, ``codex-subscription``, …)
        the lane admits. Empty frozenset = fail closed (the
        resolver / executor reject every model id).
    :param credential_env_var: SUBSCRIPTION lanes only. Name of
        the env var whose boolean presence is recorded in the
        catalog as ``credentials_present``. Never the value —
        the catalog carries a boolean only.
    :param region_labels: SUBSCRIPTION lanes only. Mapping of
        provider id → human-readable region label, used to
        disambiguate ``international`` vs ``China`` entries.
    :param label_suffix: SUBSCRIPTION lanes only. Suffix appended
        to each model label, e.g. ``" — Token Plan /
        Subscription ({region})"``.
    """

    resolver_id: str
    catalog_path: Path
    display_provider: str
    tier: str
    kind: str
    billing_risk: str
    empty_state_message: str
    lane_variant: LaneVariant
    source_label: str | None = None
    allowed_provider_prefixes: frozenset[str] = frozenset()
    credential_env_var: str = ""
    region_labels: Mapping[str, str] = field(default_factory=dict)
    label_suffix: str = ""


# ── The single source of truth ──────────────────────────────────────
#
# Adding a future OpenCode-backed subscription lane is a one-line
# addition here. The resolver, the executor, the picker, and the
# docs all read from this table — no copy-paste of catalog-read /
# filter / normalize logic, no risk of drift between the picker
# allowlist and the executor allowlist, no missed step in the
# "future lane" runbook.

_CATALOG_DIR = Path.home() / ".cache" / "homelab"

OPENCODE_NATIVE_LANES: tuple[OpenCodeNativeLaneConfig, ...] = (
    # OpenCode Free — the default free lane. No allowlist, no
    # credential env var, no region labels; the catalog is
    # authoritative. Filter: ``m.get("free") is True``.
    OpenCodeNativeLaneConfig(
        resolver_id="opencode-native",
        catalog_path=_CATALOG_DIR / "opencode-free-models.json",
        display_provider="OpenCode",
        tier="free",
        kind="manual-fallback",
        billing_risk="none-observed",
        empty_state_message=(
            "Catalog not found. Run opencode models to refresh."
        ),
        lane_variant=LaneVariant.FREE,
        # Stable source string preserved across the refactor — the
        # OpenCode Web / picker keys off this in the response, and
        # legacy clients may already depend on the value.
        source_label="opencode-free-catalog",
    ),
    # MiniMax Token Plan — subscription lane, Token Plan providers
    # only. The API-metered ``minimax/`` and ``minimax-cn/`` prefixes
    # are explicitly rejected at three layers (sync, verify,
    # resolver).
    OpenCodeNativeLaneConfig(
        resolver_id="opencode-native-minimax-token-plan",
        catalog_path=_CATALOG_DIR / "opencode-minimax-token-plan-models.json",
        display_provider="MiniMax",
        tier="subscription",
        kind="token-plan",
        billing_risk="token-plan-subscription",
        empty_state_message=(
            "MiniMax Token Plan catalog not found. Run "
            "sync-opencode-minimax-token-plan-models.py to populate."
        ),
        lane_variant=LaneVariant.SUBSCRIPTION,
        # Stable source string preserved across the refactor — the
        # OpenCode Web / picker keys off this in the response, and
        # legacy clients may already depend on the value.
        source_label="opencode-minimax-token-plan-catalog",
        allowed_provider_prefixes=frozenset(
            {"minimax-coding-plan", "minimax-cn-coding-plan"}
        ),
        credential_env_var="MINIMAX_API_KEY",
        region_labels={
            "minimax-coding-plan": "international",
            "minimax-cn-coding-plan": "China",
        },
        label_suffix="Token Plan / Subscription",
    ),
    # Codex Subscription — subscription lane, today FAIL CLOSED.
    # The allowlist is intentionally EMPTY: no public OpenCode
    # Codex-subscription provider prefix is verified yet, so the
    # resolver returns ``models: []`` and the executor rejects every
    # model id. The OpenAI API-billed ``codex/`` and ``openai/``
    # prefixes are explicitly rejected at three layers (sync,
    # verify, resolver) — they can never reach this lane.
    OpenCodeNativeLaneConfig(
        resolver_id="opencode-native-codex-subscription",
        catalog_path=_CATALOG_DIR / "opencode-codex-subscription-models.json",
        display_provider="Codex",
        tier="subscription",
        kind="subscription",
        billing_risk="subscription",
        empty_state_message=(
            "Codex Subscription catalog not found. The "
            "opencode-native-codex-subscription lane has no local "
            "verified catalog yet. Configure OpenCode's Codex "
            "subscription provider locally so the catalog can be "
            "populated; this resolver NEVER falls back to the "
            "OpenAI API-billed path."
        ),
        lane_variant=LaneVariant.SUBSCRIPTION,
        # Stable source string preserved across the refactor — the
        # resolver id was renamed from ``opencode-codex-subscription``
        # to ``opencode-native-codex-subscription`` to match the
        # harness name, but the source label was kept stable for
        # backward compatibility.
        source_label="opencode-codex-subscription-catalog",
        # Empty allowlist = fail closed. Populated by
        # stage 3 of the Codex Subscription rollout — see
        # docs/opencode-codex-subscription-models.md.
        allowed_provider_prefixes=frozenset(),
        credential_env_var="CODEX_SUBSCRIPTION_AUTH",
        label_suffix="Codex Subscription",
    ),
)


_BY_RESOLVER_ID: dict[str, OpenCodeNativeLaneConfig] = {
    lane.resolver_id: lane for lane in OPENCODE_NATIVE_LANES
}


def lane_for_resolver_id(resolver_id: str) -> OpenCodeNativeLaneConfig | None:
    """Return the lane config for *resolver_id*, or ``None`` if unknown.

    Used by the resolver to look up catalog path, allowlist, and
    label data, and by the executor to look up the allowlist. The
    same function from the same table — single source of truth.

    :param resolver_id: Canonical harness id, e.g.
        ``"opencode-native-minimax-token-plan"``.
    :returns: The matching :class:`OpenCodeNativeLaneConfig`, or
        ``None`` when no lane is registered for *resolver_id*.
    """
    return _BY_RESOLVER_ID.get(resolver_id)


def lane_for_executor_harness_id(harness_id: str) -> OpenCodeNativeLaneConfig | None:
    """Return the lane config for *harness_id* (the executor's own id).

    Convenience wrapper around :func:`lane_for_resolver_id` —
    today the executor's harness id and the resolver id are the
    same string, but a future refactor that separates them needs
    only this one wrapper to change.

    :param harness_id: The harness id the executor exposes (e.g.
        ``"opencode-native-minimax-token-plan"``).
    :returns: The matching :class:`OpenCodeNativeLaneConfig`, or
        ``None`` when no lane is registered.
    """
    return _BY_RESOLVER_ID.get(harness_id)


def all_resolver_ids() -> tuple[str, ...]:
    """Return every resolver id registered in the shared config table.

    Used by the server-side
    ``_HARNESS_MODEL_PROVIDERS`` registry to wire each lane to the
    generic resolver. Returned as a tuple so callers can rely on
    stable iteration order.
    """
    return tuple(lane.resolver_id for lane in OPENCODE_NATIVE_LANES)


def empty_state_disclaimer(lane: OpenCodeNativeLaneConfig) -> str:
    """Render the full "no fallback" disclaimer for *lane*.

    The resolver's catalog-missing error is the
    ``empty_state_message`` plus this disclaimer. The disclaimer
    is generated from the lane's variant so a future FREE lane
    never claims "no paid fallback" when the message would be
    misleading.
    """
    if lane.lane_variant is LaneVariant.SUBSCRIPTION:
        return (
            f" {lane.resolver_id} NEVER falls back to a paid-API "
            f"provider; configure the local catalog to populate this lane."
        )
    return (
        f" {lane.resolver_id} never falls back to a paid API; "
        f"the catalog is the only source of models."
    )


__all__ = [
    "LaneVariant",
    "OpenCodeNativeLaneConfig",
    "OPENCODE_NATIVE_LANES",
    "all_resolver_ids",
    "empty_state_disclaimer",
    "lane_for_executor_harness_id",
    "lane_for_resolver_id",
]
