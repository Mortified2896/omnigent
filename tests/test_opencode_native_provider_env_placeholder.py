"""Targeted regression tests for the omniroute ``{env:NAME}`` placeholder.

A user-authored ``~/.config/opencode/opencode.json(c)`` may declare the
omniroute provider with ``apiKey: \"{env:OMNIROUTE_API_KEY}\"`` so the same
JSON works on different hosts. The runner-side helper used to return that
literal string verbatim, which then leaked to ``/v1/models`` as a Bearer
token and caused ``HTTP 401`` (and a generic
``native_terminal_start_failed`` in the UI). These tests pin down the
resolver behavior so the runner either resolves the placeholder against
the host env or returns ``None`` so its own env fallback chain can fire.
"""

from __future__ import annotations

import pytest

from omnigent.opencode_native_provider import (
    OMNIROUTE_PROVIDER_ID,
    _resolve_omniroute_api_key_str,
    merge_omniroute_combo_catalog,
    omniroute_api_key_from_config,
)


def test_resolve_placeholder_returns_real_env_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """The ``{env:NAME}`` wrapper resolves to the matching host env value."""
    monkeypatch.setenv("OMNIGENT_TEST_KEY", "real-secret")
    assert _resolve_omniroute_api_key_str("{env:OMNIGENT_TEST_KEY}") == "real-secret"


def test_resolve_placeholder_returns_none_when_env_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing env means ``None``, not the literal placeholder string."""
    monkeypatch.delenv("OMNIGENT_TEST_KEY", raising=False)
    assert _resolve_omniroute_api_key_str("{env:OMNIGENT_TEST_KEY}") is None


def test_resolve_placeholder_returns_none_for_empty_env_name() -> None:
    """``{env:}`` and ``{env: }`` are malformed and resolve to ``None``.

    An empty env-name (or whitespace-only) is not a resolvable placeholder.
    The resolver returns ``None`` so the caller treats it as 'no key' and
    the runner env fallback chain can take over rather than shipping a
    literal ``{env:}`` string as a Bearer token.
    """
    assert _resolve_omniroute_api_key_str("{env:}") is None, (
        "Empty env-name placeholder must resolve to None (no key)."
    )
    assert _resolve_omniroute_api_key_str("{env: }") is None, (
        "Whitespace-only env-name placeholder must resolve to None (no key)."
    )


def test_resolve_placeholder_returns_literal_for_plain_string() -> None:
    """A real api key passes through unchanged — the resolver only handles placeholders."""
    assert _resolve_omniroute_api_key_str("plain-secret-token") == "plain-secret-token"


def test_resolve_placeholder_handles_whitespace() -> None:
    """Whitespace around the placeholder name is tolerated."""
    assert _resolve_omniroute_api_key_str("{ env: NAME }") == "{ env: NAME }", (
        "Whitespace inside the wrapper is not a placeholder pattern; only the "
        "exact ``{env:NAME}`` form is. Plain strings with whitespace are returned as-is."
    )


def test_omniroute_api_key_from_config_resolves_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """End-to-end: a synthesized config with ``{env:NAME}`` resolves via env.

    Regression: before this, the helper returned ``\"{env:NAME}\"`` to the
    OmniRoute client as a Bearer token, producing 401s on
    ``fetch_omniroute_combo_models`` and the generic
    ``native_terminal_start_failed`` banner in the UI.
    """
    monkeypatch.setenv("OMNIGENT_TEST_KEY", "host-env-token")
    config = {
        "provider": {
            OMNIROUTE_PROVIDER_ID: {
                "options": {
                    "baseURL": "http://127.0.0.1:20128/v1",
                    "apiKey": "{env:OMNIGENT_TEST_KEY}",
                }
            }
        }
    }
    assert omniroute_api_key_from_config(config) == "host-env-token"


def test_omniroute_api_key_from_config_returns_none_for_unresolved_placeholder(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Unresolved placeholder returns ``None`` so the runner env fallback fires."""
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_ROUTER_API_KEY", raising=False)
    config = {
        "provider": {
            OMNIROUTE_PROVIDER_ID: {
                "options": {
                    "baseURL": "http://127.0.0.1:20128/v1",
                    "apiKey": "{env:OMNIROUTE_API_KEY}",
                }
            }
        }
    }
    assert omniroute_api_key_from_config(config) is None, (
        "Unresolved placeholder must NOT be returned as-is; "
        "it must fall back to the runner env chain."
    )


def test_merge_replaces_unresolved_placeholder_with_available_host_token(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.delenv("OMNIGENT_OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("OMNIGENT_ROUTER_API_KEY", "host-secret")
    config = {
        "provider": {OMNIROUTE_PROVIDER_ID: {"options": {"apiKey": "{env:OMNIROUTE_API_KEY}"}}}
    }

    merged = merge_omniroute_combo_catalog(
        config,
        combos={"auto/coding": {"name": "auto/coding"}},
        approved_route="auto/coding",
    )

    options = merged["provider"][OMNIROUTE_PROVIDER_ID]["options"]
    assert options["apiKey"] == "host-secret"


def test_omniroute_api_key_from_config_returns_none_without_omniroute_provider() -> None:
    """An empty/non-omniroute config returns ``None``."""
    assert omniroute_api_key_from_config({}) is None
    assert omniroute_api_key_from_config({"provider": {}}) is None
    assert (
        omniroute_api_key_from_config({"provider": {"openai": {"options": {"apiKey": "x"}}}})
        is None
    )
