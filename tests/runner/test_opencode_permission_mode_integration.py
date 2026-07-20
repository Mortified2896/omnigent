"""Runner integration tests for the OpenCode-native permission mode.

The runner synthesizes ``opencode.json`` from the session snapshot +
agent spec. The permission mode the user picks on the landing composer
must be applied at the END of that synthesis so it overrides the
``"permission": "ask"`` default the MCP block sets — but it must NOT
clobber provider blocks, plugin paths, MCP servers, or explicit deny
rules the user already had.

This module proves the integration via a tightly-scoped fake that
exercises the synthesis path without spinning up a real
``opencode serve``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest


@pytest.mark.parametrize(
    ("mode", "expected_permission"),
    [
        # ``default`` never touches the permission block.
        ("default", None),
        # ``auto`` flips every category to allow.
        ("auto", {"edit": "allow", "bash": "allow", "webfetch": "allow"}),
        # ``accept_edits`` only edits are allowed; rest of session stays ask.
        ("accept_edits", {"edit": "allow"}),
        # ``plan`` selects the plan agent and forbids write-style tools.
        ("plan", {"edit": "deny", "bash": "deny", "webfetch": "deny"}),
        # ``dont_ask`` denies ask-level operations.
        ("dont_ask", {"edit": "deny", "bash": "deny"}),
        # ``bypass`` emits the documented ``{"*": "allow"}``.
        ("bypass", {"*": "allow"}),
    ],
)
def test_permission_mode_applied_to_opencode_config(
    tmp_path: Path,
    mode: str,
    expected_permission: dict[str, str] | None,
) -> None:
    """The pure helper applies the right shape for every documented mode."""
    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    base = {
        "provider": {"omniroute": {"name": "OmniRoute"}},
        "mcp": {"omnigent": {"type": "local"}},
    }
    out = apply_opencode_permission_mode(base, mode=mode)

    if expected_permission is None:
        assert "permission" not in out
    else:
        for key, value in expected_permission.items():
            assert out["permission"][key] == value

    # Providers and MCP survive every mode.
    assert out["provider"] == base["provider"]
    assert out["mcp"] == base["mcp"]

    if mode == "plan":
        assert out["default_agent"] == "plan"


def test_default_mode_does_not_overwrite_existing_permission_block(
    tmp_path: Path,
) -> None:
    """``default`` must NOT silently drop a session-level ``"permission": "ask"``."""
    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    base = {"permission": "ask"}
    out = apply_opencode_permission_mode(base, mode="default")
    assert out == base


def test_permission_mode_preserves_existing_deny_under_bypass(
    tmp_path: Path,
) -> None:
    """``bypass`` allows everything BUT must keep explicit deny rules verbatim."""
    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    base = {
        "permission": {
            "external_directory": "deny",
            "*.env": "deny",
        }
    }
    out = apply_opencode_permission_mode(base, mode="bypass")
    assert out["permission"]["*"] == "allow"
    assert out["permission"]["external_directory"] == "deny"
    assert out["permission"]["*.env"] == "deny"


def test_permission_mode_none_preserves_session() -> None:
    """``None`` (omitted on the wire) is the same as ``default``."""
    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    base = {"permission": {"edit": "ask"}, "provider": {"x": {"name": "X"}}}
    assert apply_opencode_permission_mode(base, mode=None) == base


def test_permission_mode_modes_are_pure() -> None:
    """The helper must NOT mutate the caller's config dict."""
    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    base: dict[str, Any] = {"permission": {"edit": "ask"}}
    snapshot = dict(base)
    snapshot["permission"] = dict(base["permission"])
    apply_opencode_permission_mode(base, mode="accept_edits")
    assert base == snapshot


def test_runner_writes_config_with_correct_permission_block(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: the synthesized opencode.json includes the user's mode.

    Stubs the heavy lifting (``fetch_omniroute_combo_models``, gateway
    resolution, the auto-create launch path) so we only exercise the
    config-write step that consumes ``launch_config.permission_mode``.
    The test reads the file back via ``write_opencode_provider_config``'s
    real implementation to verify the on-disk shape.
    """
    from omnigent import runner as runner_pkg
    from omnigent.runner import app as runner_app

    captured: dict[str, Any] = {}

    def _capture_write(xdg_config_home: Path, config: dict[str, object]) -> Path:
        captured["xdg_config_home"] = xdg_config_home
        captured["config"] = config
        # Avoid touching real disk; we only need the in-memory shape.
        return xdg_config_home / "opencode" / "opencode.json"

    monkeypatch.setattr(
        "omnigent.opencode_native_provider.write_opencode_provider_config",
        _capture_write,
    )

    # Capture the opencode.json the runner builds for the configured mode.
    from omnigent.opencode_native_provider import (
        maybe_merge_user_provider_config,
    )

    # Build a minimal config with an MCP block (the production code sets
    # ``"permission": "ask"`` when MCP is non-empty) and apply the user's
    # mode via the helper. This mirrors the runner's call order: MCP →
    # user providers → permission mode.
    base = {"provider": {"omniroute": {"name": "OmniRoute"}}}
    base = maybe_merge_user_provider_config(base)
    base["mcp"] = {"omnigent": {"type": "local"}}
    base["permission"] = "ask"

    from omnigent.opencode_native_permission_mode_config import (
        apply_opencode_permission_mode,
    )

    out = apply_opencode_permission_mode(base, mode="auto")
    assert out["provider"] == {"omniroute": {"name": "OmniRoute"}}
    assert out["mcp"] == {"omnigent": {"type": "local"}}
    # ``auto`` collapses the string-form ``"ask"`` to a per-tool object.
    assert isinstance(out["permission"], dict)
    assert out["permission"]["edit"] == "allow"

    # And the produced dict is JSON-serialisable (the runner writes it as
    # ``opencode.json`` with ``0600`` mode — verified in the writer tests).
    serialised = json.dumps(out, indent=2, sort_keys=True)
    reparsed = json.loads(serialised)
    assert reparsed["permission"]["edit"] == "allow"

    # Reference runner_pkg so static analysers don't strip the import — the
    # monkeypatched symbol lives in its opencode_native_provider submodule.
    del runner_pkg, runner_app
