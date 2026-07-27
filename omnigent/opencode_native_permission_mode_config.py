"""OpenCode-native permission-mode → ``opencode.json`` config mapping.

The OpenCode-native landing composer lets the user pick a permission mode
(Default / Auto / Accept edits / Plan / Don't ask / Bypass permissions).
The server stores the picked mode on the session row; the runner reads it
at launch and materialises it into the per-session ``opencode.json`` via
:func:`apply_opencode_permission_mode`.

The mapping is intentionally pure (no I/O, no logging) so it can be unit
tested and exercised by both the launch path and the test fixtures without
spinning up OpenCode. ``opencode serve`` does NOT accept ``--auto`` (see
``opencode serve --help``); every mode is realised via the config file
instead of an unsupported flag.

The wire-level modes for OpenCode-native are:

- ``"default"``     — no override; preserves existing OpenCode behaviour.
- ``"auto"``        — auto-approve operations that would otherwise ask,
                      while leaving explicit ``"deny"`` rules denied.
- ``"accept_edits"``— edits (``permission.edit``) allowed; everything else
                      keeps the session-default ask behaviour.
- ``"plan"``        — selects OpenCode's built-in ``plan`` agent, so the
                      session cannot edit files (the agent denies writes).
- ``"dont_ask"``    — no permission dialog appears; ask-level requests are
                      rejected by the policy hook before OpenCode would
                      prompt, and explicit ``"deny"`` rules remain denied.
- ``"bypass"``      — explicit ``"permission": "allow"`` for the session.
                      Outer sandbox / network / host policy are NOT relaxed.

Only the OpenCode-native wire values are recognised here; router-side
values (``ask_before_edits`` / ``ask_before_commands`` / ``read_only`` /
``auto_accept_edits`` / ``bypass``) are ignored by this helper — the
router surfaces them through :mod:`omnigent.server.routing_agent`, and
the runner translates them through the same OpenCode config surface
separately.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# Wire values this helper understands. Anything else returns the config
# unchanged (defense in depth — the session-create validator already
# rejects unknown values with a 4xx, but a stale ``permission_mode``
# persisted on disk must not crash the runner).
_OPENCODE_PERMISSION_MODES = frozenset(
    {"default", "auto", "accept_edits", "plan", "dont_ask", "bypass"}
)

# Tool/category keys that map to OpenCode's permission object. OpenCode
# evaluates these as a flat map: the most-specific match wins, falls back
# to the ``"*"`` wildcard. ``"external_directory"`` is the workspace
# boundary; ``"doom_loop"`` is OpenCode's safety net for stuck agents;
# ``"question"`` and ``"plan_enter"`` / ``"plan_exit"`` are the in-UI
# affordances we want to leave strictly ask by default.
_TOOL_KEYS: tuple[str, ...] = (
    "edit",
    "bash",
    "webfetch",
    "websearch",
    "external_directory",
    "doom_loop",
    "question",
    "plan_enter",
    "plan_exit",
)


def apply_opencode_permission_mode(
    config: Mapping[str, Any] | None,
    *,
    mode: str | None,
) -> dict[str, Any]:
    """
    Return a new ``opencode.json`` dict with *mode* applied.

    The input ``config`` is the synthesized dict the runner is about to
    write (providers / MCP / plugin / etc.). The function returns a new
    dict and never mutates the caller's input — call sites hand it the
    runner-owned accumulator and use the return value for the final write.

    The merge is conservative: every other top-level key (``provider``,
    ``mcp``, ``plugin``, ``model``, ``$schema``, ...) flows through
    untouched. Only ``permission`` and ``default_agent`` are touched.

    :param config: The synthesized opencode config (possibly empty). May
        already carry a ``permission`` block; existing rules are merged
        according to each mode's semantics (see module docstring).
    :param mode: Wire-level OpenCode-native permission mode, or ``None``.
    :returns: A new ``opencode.json`` dict ready to be serialised.
    """
    base = dict(config) if config else {}
    # Strip any whitespace; treat empty string as None (the runner's
    # launcher normalises to None but persistence may leave blanks).
    normalized = mode.strip() if isinstance(mode, str) else None
    if not normalized or normalized == "default":
        return base
    if normalized not in _OPENCODE_PERMISSION_MODES:
        # Unknown / router-only value. Leave the config untouched so a
        # router-emitted ``auto_accept_edits`` / ``read_only`` etc. does
        # not silently degrade into a default session.
        return base

    if normalized == "bypass":
        # Equivalent to the user-facing "Bypass permissions" toggle: every
        # tool is allowed. Omnigent's outer sandbox / network / host
        # policy stay in force — this only relaxes OpenCode's local
        # permission gate. Explicit ``"deny"`` entries are kept verbatim
        # (OpenCode evaluates per-key, so a "deny" remains a deny).
        existing = base.get("permission")
        merged: dict[str, str] = {"*": "allow"}
        if isinstance(existing, dict):
            for key, value in existing.items():
                if not isinstance(key, str) or not isinstance(value, str):
                    continue
                if value == "deny":
                    merged[key] = "deny"
        base["permission"] = merged
        return base

    if normalized == "dont_ask":
        # Never prompt the user. Ask-level requests are rejected (the
        # policy plugin returns ``reject`` for every ``permission.asked``
        # event when the session carries this mode), and explicit deny
        # rules stay denied. Read-only safe-by-default map; ``bash`` /
        # ``edit`` are denied so the policy hook has nothing to ask
        # about. The session can still read files and answer questions.
        base["permission"] = _merge_deny_default(
            base.get("permission"),
            deny=("edit", "bash", "webfetch", "websearch", "doom_loop"),
        )
        return base

    if normalized == "accept_edits":
        # Edit operations are auto-approved; everything else keeps the
        # session-default ask behaviour. OpenCode's per-tool evaluation
        # means explicit denies elsewhere are still respected.
        base["permission"] = _merge_with_override(
            base.get("permission"),
            overrides={"edit": "allow"},
        )
        return base

    if normalized == "auto":
        # Auto-approve operations that would otherwise ask, but never
        # override an explicit deny. Realised by relaxing every category
        # to ``allow`` while keeping any existing ``deny`` keys verbatim.
        existing = base.get("permission")
        merged: dict[str, str] = {}
        if isinstance(existing, dict):
            for key, value in existing.items():
                if isinstance(key, str) and isinstance(value, str):
                    merged[key] = value
        for key in _TOOL_KEYS:
            if merged.get(key) == "deny":
                continue
            merged[key] = "allow"
        merged.setdefault("*", "allow")
        base["permission"] = merged
        return base

    if normalized == "plan":
        # Select OpenCode's built-in plan agent. The agent itself refuses
        # writes, so editing the workspace is impossible even though no
        # tool permission is denied explicitly. We additionally set a
        # ``permission: deny`` for ``edit`` so any accidental agent
        # override (custom agent configs that swap the agent back to
        # ``build``) cannot bypass the user-visible Plan intent.
        base["default_agent"] = "plan"
        base["permission"] = _merge_deny_default(
            base.get("permission"),
            deny=("edit", "bash", "webfetch", "websearch"),
        )
        return base

    # Unreachable: all enum members handled above.
    return base


def _merge_with_override(
    existing: Any,
    *,
    overrides: Mapping[str, str],
) -> dict[str, str]:
    """
    Return a fresh ``permission`` dict with *overrides* applied on top of *existing*.

    Existing ``"deny"`` entries are preserved verbatim — they are
    user-policy commitments and must not be relaxed by a mode override.
    Existing keys not in *overrides* are preserved as-is (the user's
    personal provider / tool rules survive the mode change).
    """
    merged: dict[str, str] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(key, str) and isinstance(value, str):
                merged[key] = value
    for key, value in overrides.items():
        if merged.get(key) == "deny":
            continue
        merged[key] = value
    return merged


def _merge_deny_default(
    existing: Any,
    *,
    deny: tuple[str, ...],
) -> dict[str, str]:
    """
    Return a fresh ``permission`` dict with the listed keys forced to ``"deny"``.

    Non-listed keys from *existing* are preserved; the session's
    explicit allow rules (e.g. ``external_directory: allow`` for an
    opencode-managed XDG path) survive intact.
    """
    merged: dict[str, str] = {}
    if isinstance(existing, dict):
        for key, value in existing.items():
            if isinstance(key, str) and isinstance(value, str):
                merged[key] = value
    for key in deny:
        merged[key] = "deny"
    return merged


def opencode_permission_modes() -> frozenset[str]:
    """Return the set of OpenCode-native wire-level permission modes."""
    return _OPENCODE_PERMISSION_MODES
