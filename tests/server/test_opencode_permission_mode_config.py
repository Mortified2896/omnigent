"""Unit tests for :mod:`omnigent.opencode_native_permission_mode_config`.

The OpenCode-native landing composer surfaces six user-visible permission
modes; each maps to a concrete mutation of the synthesized
``opencode.json`` dict the runner writes. These tests pin down the
mapping table and the merge semantics (preserving providers / MCP /
plugins / explicit deny rules).
"""

from __future__ import annotations

import pytest

from omnigent.opencode_native_permission_mode_config import (
    apply_opencode_permission_mode,
    opencode_permission_modes,
)

# ---------------------------------------------------------------------------
# Default / unknown
# ---------------------------------------------------------------------------


def test_default_leaves_config_unchanged() -> None:
    """``default`` (and the implicit ``None``) must never touch the config."""
    config = {
        "$schema": "https://opencode.ai/config.json",
        "permission": {"edit": "ask"},
        "provider": {"omniroute": {"name": "OmniRoute"}},
        "mcp": {"omnigent": {"type": "local"}},
        "plugin": ["/tmp/p.js"],
    }
    out = apply_opencode_permission_mode(config, mode="default")
    assert out == config
    # ``None`` is the same as no mode.
    out_none = apply_opencode_permission_mode(config, mode=None)
    assert out_none == config


def test_unknown_mode_returns_config_unchanged() -> None:
    """A stale / router-only value must not silently degrade the session."""
    config = {"permission": {"edit": "ask"}}
    # ``read_only`` is a router-side mode, not an OpenCode-native UI value.
    assert apply_opencode_permission_mode(config, mode="read_only") == config
    # Truly unknown.
    assert apply_opencode_permission_mode(config, mode="nonsense") == config
    # Empty / whitespace -> default.
    assert apply_opencode_permission_mode(config, mode="") == config
    assert apply_opencode_permission_mode(config, mode="   ") == config


def test_empty_config_handled() -> None:
    """An empty config dict is a valid input — the helper must not crash."""
    out = apply_opencode_permission_mode({}, mode="bypass")
    assert out == {"permission": {"*": "allow"}}


def test_does_not_mutate_caller_dict() -> None:
    """The helper returns a new dict; the caller's input is preserved."""
    config = {"permission": {"edit": "ask"}}
    snapshot = dict(config)
    apply_opencode_permission_mode(config, mode="accept_edits")
    assert config == snapshot


# ---------------------------------------------------------------------------
# Auto
# ---------------------------------------------------------------------------


def test_auto_allows_every_category_without_clobbering_denies() -> None:
    """Auto allows tools that would normally ask but keeps explicit denies."""
    config = {
        "permission": {
            "external_directory": "deny",
            "read": "ask",
        }
    }
    out = apply_opencode_permission_mode(config, mode="auto")
    assert out["permission"]["external_directory"] == "deny"
    assert out["permission"]["edit"] == "allow"
    assert out["permission"]["bash"] == "allow"
    assert out["permission"]["webfetch"] == "allow"
    assert out["permission"]["*"] == "allow"


def test_auto_does_not_touch_provider_or_mcp() -> None:
    """Auto is permission-only; providers / MCP / plugins survive."""
    config = {
        "provider": {"omniroute": {"name": "OmniRoute"}},
        "mcp": {"omnigent": {"type": "local"}},
        "plugin": ["/tmp/p.js"],
        "permission": {"external_directory": "deny"},
    }
    out = apply_opencode_permission_mode(config, mode="auto")
    assert out["provider"] == config["provider"]
    assert out["mcp"] == config["mcp"]
    assert out["plugin"] == config["plugin"]


# ---------------------------------------------------------------------------
# Accept edits
# ---------------------------------------------------------------------------


def test_accept_edits_allows_edit_only() -> None:
    """Only ``edit`` is overridden; bash / webfetch / etc. stay at their default."""
    config: dict = {}
    out = apply_opencode_permission_mode(config, mode="accept_edits")
    assert out["permission"] == {"edit": "allow"}


def test_accept_edits_preserves_existing_deny() -> None:
    """An existing ``edit: deny`` is not relaxed by Accept edits."""
    config = {"permission": {"edit": "deny", "bash": "ask"}}
    out = apply_opencode_permission_mode(config, mode="accept_edits")
    assert out["permission"]["edit"] == "deny"
    assert out["permission"]["bash"] == "ask"


def test_accept_edits_merges_into_existing_config() -> None:
    """Existing keys survive; ``edit`` is set to ``allow``."""
    config = {"permission": {"bash": "ask", "external_directory": "deny"}}
    out = apply_opencode_permission_mode(config, mode="accept_edits")
    assert out["permission"]["edit"] == "allow"
    assert out["permission"]["bash"] == "ask"
    assert out["permission"]["external_directory"] == "deny"


# ---------------------------------------------------------------------------
# Plan
# ---------------------------------------------------------------------------


def test_plan_selects_plan_agent_and_denies_writes() -> None:
    """Plan selects ``default_agent: plan`` and denies write-style tools."""
    config: dict = {}
    out = apply_opencode_permission_mode(config, mode="plan")
    assert out["default_agent"] == "plan"
    assert out["permission"]["edit"] == "deny"
    assert out["permission"]["bash"] == "deny"
    assert out["permission"]["webfetch"] == "deny"


def test_plan_does_not_silently_switch_back_to_build() -> None:
    """A previously-set default_agent must be replaced, not preserved."""
    config = {"default_agent": "build", "permission": {"edit": "ask"}}
    out = apply_opencode_permission_mode(config, mode="plan")
    assert out["default_agent"] == "plan"


# ---------------------------------------------------------------------------
# Don't ask
# ---------------------------------------------------------------------------


def test_dont_ask_denies_ask_level_operations() -> None:
    """Don't ask rejects operations that would normally require asking."""
    config: dict = {}
    out = apply_opencode_permission_mode(config, mode="dont_ask")
    assert out["permission"]["edit"] == "deny"
    assert out["permission"]["bash"] == "deny"
    assert out["permission"]["webfetch"] == "deny"
    assert out["permission"]["websearch"] == "deny"
    assert out["permission"]["doom_loop"] == "deny"


def test_dont_ask_does_not_map_to_bypass() -> None:
    """``dont_ask`` must NOT be a synonym for ``bypass`` / auto-allow."""
    config: dict = {}
    out = apply_opencode_permission_mode(config, mode="dont_ask")
    assert out["permission"].get("*") != "allow"
    # No key is upgraded to ``allow``.
    assert "allow" not in out["permission"].values()


# ---------------------------------------------------------------------------
# Bypass
# ---------------------------------------------------------------------------


def test_bypass_sets_global_allow() -> None:
    """Bypass emits the documented ``{"*": "allow"}`` form."""
    out = apply_opencode_permission_mode({}, mode="bypass")
    assert out["permission"] == {"*": "allow"}


def test_bypass_preserves_explicit_deny() -> None:
    """An existing ``external_directory: deny`` survives Bypass."""
    config = {"permission": {"external_directory": "deny"}}
    out = apply_opencode_permission_mode(config, mode="bypass")
    assert out["permission"]["external_directory"] == "deny"
    assert out["permission"]["*"] == "allow"


# ---------------------------------------------------------------------------
# Mode enumeration
# ---------------------------------------------------------------------------


def test_opencode_permission_modes_includes_all_six() -> None:
    """The user-visible selector exposes exactly six modes."""
    modes = opencode_permission_modes()
    assert modes == frozenset({"default", "auto", "accept_edits", "plan", "dont_ask", "bypass"})


@pytest.mark.parametrize("mode", ["default", "auto", "accept_edits", "plan", "dont_ask", "bypass"])
def test_all_six_modes_return_a_dict(mode: str) -> None:
    """Each documented mode returns a dict (never None / raises)."""
    config = {"provider": {"x": {"name": "X"}}}
    out = apply_opencode_permission_mode(config, mode=mode)
    assert isinstance(out, dict)
    assert out["provider"] == config["provider"]
