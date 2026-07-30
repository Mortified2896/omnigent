"""Runner pre-launch revalidation gate for the OpenCode delegation selector.

Issue #56 documents the production failure where the harness swaps
to ``claude-sdk`` between initial resolution and child launch
(e.g. after a runner reconnect / spec-cache eviction / bundle swap).
The gate in :func:`omnigent.runner.app._resolve_harness_config` is
the only safety net; these tests pin its behaviour at three
boundaries:

- A child launch with ``resolved_harness == "opencode-native"`` is
  allowed and emits a single structured log line.
- A child launch with any other ``resolved_harness`` raises
  :class:`_OpencodeDelegationRejection` carrying the structured
  reason — no dispatch to the wrong harness, no silent fall-through.
- The revalidation runs ONLY for child sessions whose persisted
  ``delegation_provenance`` carries the OpenCode selector's
  ``resolved`` decision. Other harnesses (claude-native,
  codex-native, claude-sdk) flow through unchanged.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent.agent_selector import (
    SEMANTIC_OPENCODE_HARNESS,
    SEMANTIC_OPENCODE_SELECTOR,
)
from omnigent.runner.app import (
    _OpencodeDelegationRejection,
    _revalidate_opencode_delegation_at_launch,
)


@pytest.mark.asyncio()
async def test_opencode_native_launch_passes_pre_launch_revalidation(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A child launch on the canonical opencode-native harness passes.

    The revalidation is a no-op success path for the canonical case;
    it emits a single structured log line so SRE can grep durable
    history ("opencode_delegation.pre_launch_resolved"). No
    exception is raised.
    """
    caplog.set_level(logging.INFO, logger="omnigent.runner.app")

    await _revalidate_opencode_delegation_at_launch(
        session_id="conv_opencode_child",
        agent_id="ag_opencode_native",
        resolved_harness="opencode-native",
        sub_agent_name=None,
        provenance={
            "selector": SEMANTIC_OPENCODE_SELECTOR,
            "decision": "resolved",
            "resolved_agent_id": "ag_opencode_native",
            "resolved_harness": SEMANTIC_OPENCODE_HARNESS,
        },
    )

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "opencode_delegation.pre_launch_resolved" in joined
    assert "conv_opencode_child" in joined
    assert "opencode-native" in joined


@pytest.mark.asyncio()
async def test_non_opencode_harness_is_refused_on_opencode_session() -> None:
    """A child session carrying OpenCode provenance MUST refuse a non-OpenCode harness.

    Production evidence: this is exactly the silent fall-through that
    produced the bug — the resolved identity is "opencode-native-ui /
    opencode-native" but the harness the runner is about to launch is
    "claude-sdk" (because the parent's spec resolved first after a
    reconnect). The gate refuses the launch with a structured reason
    rather than silently dispatching to Verity.
    """
    with pytest.raises(_OpencodeDelegationRejection) as exc_info:
        await _revalidate_opencode_delegation_at_launch(
            session_id="conv_opencode_child",
            agent_id="ag_opencode_native",
            resolved_harness="claude-sdk",
            sub_agent_name="opencode",
            provenance={
                "selector": SEMANTIC_OPENCODE_SELECTOR,
                "decision": "resolved",
                "resolved_agent_id": "ag_opencode_native",
                "resolved_harness": SEMANTIC_OPENCODE_HARNESS,
            },
        )

    assert exc_info.value.reason == "opencode_delegation_harness_mismatch"
    assert exc_info.value.resolved_harness == "claude-sdk"
    assert "opencode-native" in str(exc_info.value)
    assert "conv_opencode_child" in str(exc_info.value)


@pytest.mark.asyncio()
async def test_revalidation_skips_for_non_opencode_session() -> None:
    """Other harnesses flow through the gate unchanged.

    The gate is an additive safety net for the OpenCode delegation
    path. A claude-native / codex-native / claude-sdk child launch
    is not subject to the OpenCode revalidation — those harnesses
    have their own identity contracts (sub-agent swap, switch-agent,
    etc.) and the OpenCode resolver MUST not interfere with them.
    """
    # ``resolved_harness="claude-sdk"`` is the parent's harness for
    # a Verity session — the gate's resolved_harness check is the
    # ONLY discriminator. Today the gate fires for any non-
    # ``opencode-native`` harness on an opencode-provenance session,
    # not for non-opencode-provenance sessions; in this test we
    # pass ``session_id=None`` so the gate short-circuits without
    # touching the server snapshot.
    await _revalidate_opencode_delegation_at_launch(
        session_id=None,
        agent_id=None,
        resolved_harness="claude-sdk",
        sub_agent_name="researcher",
    )
    # No exception — the gate short-circuits when there's no
    # session to consult. This is the legacy / non-delegation path
    # that the OpenCode resolver does NOT change.


@pytest.mark.asyncio()
async def test_revalidation_logs_warning_on_harness_mismatch(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The gate emits a structured warning before raising.

    The warning carries the session id + resolved harness so a SRE
    reading the parent conversation's failure trace sees exactly
    which session was refused and for which harness.
    """
    caplog.set_level(logging.WARNING, logger="omnigent.runner.app")

    with pytest.raises(_OpencodeDelegationRejection):
        await _revalidate_opencode_delegation_at_launch(
            session_id="conv_opencode_child",
            agent_id="ag_opencode_native",
            resolved_harness="claude-sdk",
            sub_agent_name=None,
            provenance={
                "selector": SEMANTIC_OPENCODE_SELECTOR,
                "decision": "resolved",
                "resolved_agent_id": "ag_opencode_native",
                "resolved_harness": SEMANTIC_OPENCODE_HARNESS,
            },
        )

    joined = "\n".join(record.getMessage() for record in caplog.records)
    assert "opencode_delegation.harness_mismatch" in joined
    assert "conv_opencode_child" in joined
    assert "claude-sdk" in joined


# Sanity check: the rejection exception class carries the structured
# reason as an attribute so a future parent-session error event can
# include it directly without parsing the message string.
def test_rejection_carries_structured_attributes() -> None:
    exc = _OpencodeDelegationRejection(
        "test message",
        session_id="conv_xyz",
        agent_id="ag_xyz",
        resolved_harness="claude-sdk",
        reason="opencode_delegation_harness_mismatch",
    )
    assert exc.reason == "opencode_delegation_harness_mismatch"
    assert exc.session_id == "conv_xyz"
    assert exc.agent_id == "ag_xyz"
    assert exc.resolved_harness == "claude-sdk"
    assert str(exc) == "test message"
