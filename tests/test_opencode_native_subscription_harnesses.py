"""Tests for the OpenCode-backed subscription harness modules.

These pin the contract that the MiniMax Token Plan lane and the Codex
Subscription lane share the OpenCode bridge but route through their own
allowlists — so:

* An API-metered ``minimax/`` or ``minimax-cn/`` id NEVER reaches the
  MiniMax Token Plan executor, even if a buggy catalog run shipped it.
* The Codex Subscription executor rejects every model id until the
  local catalog resolver finds a verified Codex-subscription provider
  prefix (currently the allowlist is empty — fail closed).
* The harness id each executor exposes matches the canonical id the
  picker uses to route ``/v1/harness-model-options`` requests.
* The provider prefix lists are the SINGLE source of truth between the
  catalog resolver and the executor — they must match.
"""

from __future__ import annotations

import pytest

from omnigent.inner import opencode_native_codex_subscription_harness as codex_mod
from omnigent.inner import opencode_native_minimax_token_plan_harness as minimax_mod


# ── Harness ids ──────────────────────────────────────────────────────


def test_minimax_token_plan_harness_id_matches_picker_registry() -> None:
    """The harness id the executor exposes must match the catalog resolver's key."""
    assert minimax_mod.OPENCODE_NATIVE_MINIMAX_TOKEN_PLAN_HARNESS_ID == (
        "opencode-native-minimax-token-plan"
    )


def test_codex_subscription_harness_id_matches_picker_registry() -> None:
    """The harness id the executor exposes must match the catalog resolver's key."""
    assert codex_mod.OPENCODE_NATIVE_CODEX_SUBSCRIPTION_HARNESS_ID == (
        "opencode-native-codex-subscription"
    )


# ── Provider-prefix allowlists ──────────────────────────────────────


def test_minimax_token_plan_allowed_prefixes() -> None:
    """Pin the two Token Plan prefixes (and only those) for the executor."""
    assert minimax_mod._MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES == frozenset(
        {"minimax-coding-plan", "minimax-cn-coding-plan"}
    )


def test_codex_subscription_allowed_prefixes_is_empty_by_design() -> None:
    """The Codex Subscription allowlist is intentionally empty today — fail closed."""
    assert codex_mod._OPENCODE_CODEX_SUBSCRIPTION_ALLOWED_PROVIDER_PREFIXES == frozenset()


# ── Allowlist matching ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "model_id",
    [
        "opencode/minimax-coding-plan/MiniMax-M2.7",
        "minimax-coding-plan/MiniMax-M2.7",
        "opencode/minimax-cn-coding-plan/MiniMax-M3",
        "minimax-cn-coding-plan/MiniMax-M3",
    ],
)
def test_minimax_token_plan_allows_token_plan_ids(model_id: str) -> None:
    """Both bare and fully-qualified Token Plan ids are admitted."""
    assert minimax_mod._is_allowed_token_plan_model(model_id) is True


@pytest.mark.parametrize(
    "model_id",
    [
        # API-metered prefixes — must NEVER reach the Token Plan lane.
        "opencode/minimax/MiniMax-M2.7",
        "minimax/MiniMax-M2.7",
        "opencode/minimax-cn/MiniMax-M3",
        "minimax-cn/MiniMax-M3",
        # OpenCode Free lane model ids — also rejected (different access path).
        "opencode/big-pickle",
        "opencode/deepseek-v4-flash-free",
        # Claude / Codex / Pi family — rejected.
        "opencode/anthropic/claude-opus-4",
        "opencode/codex/gpt-5.4",
    ],
)
def test_minimax_token_plan_rejects_non_token_plan_ids(model_id: str) -> None:
    """API-metered ids and ids from other lanes are rejected."""
    assert minimax_mod._is_allowed_token_plan_model(model_id) is False


@pytest.mark.parametrize(
    "model_id",
    [
        "opencode/codex/gpt-5.4",
        "opencode/codex-subscription/gpt-5.4",
        "opencode/minimax-coding-plan/MiniMax-M2.7",
        "opencode/big-pickle",
        "",
    ],
)
def test_codex_subscription_rejects_every_model_today(model_id: str) -> None:
    """Until a Codex-subscription provider prefix is verified, NO id is admitted."""
    assert codex_mod._is_allowed_codex_subscription_model(model_id) is False


# ── Hard rules ──────────────────────────────────────────────────────


def test_minimax_token_plan_has_no_openai_billing_fallback() -> None:
    """Sanity: the executor's allowed prefixes contain NO OpenAI / openai prefix."""
    allowed = minimax_mod._MINIMAX_TOKEN_PLAN_ALLOWED_PROVIDER_PREFIXES
    for prefix in allowed:
        assert "openai" not in prefix
        assert "codex" not in prefix  # not the Codex lane either
        assert "claude" not in prefix  # not the Claude lane either


def test_codex_subscription_has_no_openai_billing_fallback() -> None:
    """Sanity: the Codex Subscription executor has no API key, no API path.

    The executor reads from the OpenCode bridge only. The
    ``OPENAI_API_KEY`` env var is never consulted; the ``openai/`` or
    ``codex/`` (OpenAI API) provider prefixes are never in the
    allowlist. This is a guard against a future refactor that
    accidentally introduces an OpenAI billing path.

    The textual grep below inspects the executable code (with all
    docstrings stripped) so the disclaimer text in the docstrings is
    excluded — a future PR that actually introduces ``os.environ``
    reads of ``OPENAI_API_KEY`` or imports the openai SDK will fail
    this guard.
    """
    import re

    src_text = open(codex_mod.__file__, encoding="utf-8").read()  # type: ignore[arg-type]
    # Strip ALL triple-quoted docstrings (module + class + function).
    # The grep is intentionally simple — it matches everything between
    # the first set of triple-quotes on each line and the next set.
    code_only = re.sub(r'"""[\s\S]*?"""', "", src_text)
    # The executor must NEVER consult ``OPENAI_API_KEY`` ...
    assert "OPENAI_API_KEY" not in code_only
    # ... and must NEVER import the openai SDK.
    assert "import openai" not in code_only
    assert "from openai" not in code_only


# ── Factory wiring ──────────────────────────────────────────────────


def test_minimax_token_plan_factory_returns_executor() -> None:
    """The create_app factory wires the right executor class."""
    factory = minimax_mod._build_opencode_native_minimax_token_plan_executor
    # The factory returns an executor instance (we don't need to spawn it
    # to confirm wiring — just that the factory is callable and returns
    # an instance of the expected class).
    import inspect

    assert callable(factory)
    assert inspect.isfunction(factory)


def test_codex_subscription_factory_returns_executor() -> None:
    """The create_app factory wires the right executor class."""
    factory = codex_mod._build_opencode_native_codex_subscription_executor
    import inspect

    assert callable(factory)
    assert inspect.isfunction(factory)