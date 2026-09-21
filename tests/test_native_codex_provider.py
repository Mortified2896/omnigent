"""Tests for native-Codex provider routing (configure harnesses parity).

Covers :func:`omnigent.inner.codex_executor._provider_codex_config_overrides`
and :func:`omnigent.harnesses.codex_native.app_server.resolve_native_codex_launch` —
the path that makes a native Codex terminal route through a ``configure
harness`` provider just like the in-process codex harness, instead of only
the Databricks ucode profile. Providers are constructed via the real config
parser; config + ambient are isolated so resolution is deterministic.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest
import yaml

from omnigent.errors import OmnigentError
from omnigent.harnesses.codex_native.app_server import resolve_native_codex_launch
from omnigent.inner.codex_executor import _provider_codex_config_overrides
from omnigent.spec.types import AgentSpec, ExecutorSpec, ProviderAuth


@pytest.fixture()
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate config + ambient so codex routing resolution is deterministic."""
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in (
        "ANTHROPIC_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "ZAI_API_KEY",
        "OMNIROUTE_O3_KEY",
        "CODEX_HOME",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("DATABRICKS_CONFIG_PROFILE", raising=False)
    return tmp_path


def _seed(config_home: Path, providers: dict[str, object]) -> None:
    """Write a ``providers:`` block into the isolated config home."""
    (config_home / "config.yaml").write_text(yaml.safe_dump({"providers": providers}))


def _write_codex_login(home: Path, *, logged_in: bool) -> None:
    """Write (or stub-empty) ``~/.codex/auth.json`` under the isolated HOME.

    The native subscription routing resolves ``CODEX_HOME or ~/.codex`` — with
    HOME redirected to *home* by the ``_isolated`` fixture, this controls
    whether Codex is considered logged in.

    :param home: The isolated HOME directory (the ``_isolated`` fixture value).
    :param logged_in: When ``True``, write an apikey-mode credential so
        ``codex_auth_has_credential`` returns ``True``; when ``False``, write an
        empty ``{}`` (present-but-logged-out) so it returns ``False``.
    """
    codex_dir = home / ".codex"
    codex_dir.mkdir(parents=True, exist_ok=True)
    content = '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-login"}' if logged_in else "{}"
    (codex_dir / "auth.json").write_text(content, encoding="utf-8")


def test_explicit_omniroute_lane_uses_configured_default_and_model(_isolated: Path) -> None:
    """The lane selects the known configured default without URL/name heuristics."""
    _seed(
        _isolated,
        {
            "production-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://gateway.example.test/v1",
                    "api_key": "test-placeholder",
                },
            },
            "codex-sub": {"kind": "subscription", "cli": "codex"},
        },
    )

    launch = resolve_native_codex_launch(
        model="codex/gpt-5.6-luna",
        access_lane="omniroute",
    )

    joined = "\n".join(launch.config_overrides)
    assert launch.model == "codex/gpt-5.6-luna"
    assert 'model_provider="omnigent_provider"' in joined
    assert "gateway.example.test" in joined
    assert "production-gateway" in launch.summary
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "omniroute"
    assert launch.trace_provenance.provider == "production-gateway"
    assert launch.trace_provenance.provider_fallback is None


def test_explicit_codex_direct_lane_forces_subscription_transport(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Direct Codex always pins the built-in OpenAI subscription provider."""
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.codex_auth_has_credential", lambda _path: True
    )
    _seed(
        _isolated,
        {
            "omniroute": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://127.0.0.1:20128/v1",
                    "api_key": "test-placeholder",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model="gpt-5.5", access_lane="codex-direct")

    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.model == "gpt-5.5"
    assert launch.profile is None
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "codex-direct"
    assert launch.trace_provenance.provider == "openai-codex-subscription"
    assert launch.trace_provenance.provider_fallback is False


def test_explicit_codex_direct_lane_fails_without_login(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.codex_auth_has_credential", lambda _path: False
    )

    with pytest.raises(OmnigentError, match=r"^Codex Subscription — Direct is not authenticated$"):
        resolve_native_codex_launch(model="gpt-5.5", access_lane="codex-direct")


def test_explicit_lane_rejects_missing_model(_isolated: Path) -> None:
    with pytest.raises(OmnigentError, match="requires an explicit model"):
        resolve_native_codex_launch(model=None, access_lane="omniroute")


def test_direct_lane_accepts_model_id_also_used_by_omniroute(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lane identity, not model spelling, selects the subscription transport."""
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.codex_auth_has_credential", lambda _path: True
    )

    launch = resolve_native_codex_launch(
        model="codex/gpt-5.5",
        access_lane="codex-direct",
    )

    assert launch.model == "codex/gpt-5.5"
    assert launch.config_overrides == ['model_provider="openai"']


def test_omniroute_lane_fails_clearly_without_configured_default(_isolated: Path) -> None:
    with pytest.raises(OmnigentError, match=r"^OmniRoute lane unavailable$"):
        resolve_native_codex_launch(model="gpt-5.5", access_lane="omniroute")


def test_provider_codex_overrides_coerce_chat_wire_to_responses() -> None:
    """A ``chat`` provider wire is coerced to ``responses`` in the override.

    codex >= 0.137 hard-fails config load on ``wire_api="chat"``
    (``wire_api = "chat" is no longer supported``), so emitting it would break
    OSS / OpenRouter routing outright. ``responses`` is the only wire codex
    still accepts, so the override must carry it even for a chat-configured
    provider. Failure (a literal ``wire_api="chat"`` line) means a native Codex
    launch would refuse to start.
    """
    overrides = _provider_codex_config_overrides(
        model="qwen/qwen3.7-plus",
        base_url="https://openrouter.ai/api/v1",
        auth_command="printf %s sk-or-test",
        wire_api="chat",
    )
    joined = "\n".join(overrides)
    assert 'model="qwen/qwen3.7-plus"' in joined
    assert 'model_provider="omnigent_provider"' in joined
    assert 'base_url="https://openrouter.ai/api/v1"' in joined
    # chat is coerced to responses; codex >= 0.137 rejects a chat config.
    assert 'wire_api="responses"' in joined
    assert 'wire_api="chat"' not in joined
    # The token command is embedded as the sh auth command.
    assert "printf %s sk-or-test" in joined


def test_provider_codex_overrides_preserve_responses_wire() -> None:
    """An explicit ``responses`` wire passes through unchanged."""
    overrides = _provider_codex_config_overrides(
        model="gpt-5.5",
        base_url="https://api.openai.com/v1",
        auth_command="printf %s sk",
        wire_api="responses",
    )
    assert 'wire_api="responses"' in "\n".join(overrides)


def test_provider_codex_overrides_use_env_key_without_secret() -> None:
    """An env-backed provider names the variable and never embeds its value."""
    overrides = _provider_codex_config_overrides(
        model="custom/o3-route-deadbeef",
        base_url="http://127.0.0.1:20128/v1",
        env_key="OMNIROUTE_O3_KEY",
        wire_api="responses",
    )

    joined = "\n".join(overrides)
    assert 'env_key="OMNIROUTE_O3_KEY"' in joined
    assert "request_max_retries=4" in joined
    assert "stream_max_retries=5" in joined
    assert "stream_idle_timeout_ms=300000" in joined
    assert "auth=" not in joined
    assert "sentinel-bearer" not in joined


def test_resolve_env_ref_launch_preserves_only_variable_name(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Native routing keeps an env reference instead of materializing its bearer."""
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "sentinel-bearer-must-not-persist")
    _seed(
        _isolated,
        {
            "o3-omniroute": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "http://127.0.0.1:20128/v1",
                    "api_key_ref": "env:OMNIROUTE_O3_KEY",
                    "wire_api": "responses",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(
        model="custom/o3-route-deadbeef",
        access_lane="omniroute",
    )

    joined = "\n".join(launch.config_overrides)
    assert launch.env_passthrough == ("OMNIROUTE_O3_KEY",)
    assert 'env_key="OMNIROUTE_O3_KEY"' in joined
    assert "sentinel-bearer-must-not-persist" not in joined


def test_explicit_o3_lane_uses_loopback_env_when_global_provider_is_absent(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "sentinel-bearer-must-not-persist")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    launch = resolve_native_codex_launch(
        model="custom/o3-route-deadbeef",
        access_lane="omniroute",
    )

    joined = "\n".join(launch.config_overrides)
    assert launch.env_passthrough == ("OMNIROUTE_O3_KEY",)
    assert 'base_url="http://127.0.0.1:20128/v1"' in joined
    assert 'env_key="OMNIROUTE_O3_KEY"' in joined
    assert "sentinel-bearer-must-not-persist" not in joined


def test_explicit_o3_lane_rejects_non_loopback_fallback(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "test-key")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "https://example.invalid")

    with pytest.raises(OmnigentError, match="OmniRoute lane unavailable"):
        resolve_native_codex_launch(
            model="custom/o3-route-deadbeef",
            access_lane="omniroute",
        )


def test_provider_codex_overrides_omit_model_line_when_none() -> None:
    """``model=None`` omits the ``model="..."`` line but still routes."""
    overrides = _provider_codex_config_overrides(
        model=None,
        base_url="https://api.openai.com/v1",
        auth_command="printf %s sk",
        wire_api="responses",
    )
    joined = "\n".join(overrides)
    assert "model=" not in joined.replace("model_provider=", "")  # no bare model= line
    assert 'model_provider="omnigent_provider"' in joined


def test_resolve_native_codex_launch_key_default_routes_via_overrides(
    _isolated: Path,
) -> None:
    """An openai key default → provider overrides, profile None.

    The P0 parity: native Codex honors `configure harnesses`. Failure means
    the native launch ignored the configured provider.
    """
    _seed(
        _isolated,
        {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-default",
                    "models": {"default": "gpt-5.5"},
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)
    assert launch.profile is None  # a provider routes via overrides, not a profile
    assert launch.model == "gpt-5.5"
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert "printf %s sk-oai-default" in joined


def test_resolve_native_codex_launch_openrouter_coerces_chat_wire(_isolated: Path) -> None:
    """A chat-configured gateway (OpenRouter) routes with the coerced responses wire.

    The provider is persisted with ``wire_api: chat``, but codex >= 0.137 can
    no longer load a chat config, so the resolved launch overrides must carry
    ``wire_api="responses"`` (the coercion in ``_provider_codex_config_overrides``).
    """
    _seed(
        _isolated,
        {
            "openrouter": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "sk-or",
                    "wire_api": "chat",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model="qwen/q")
    joined = "\n".join(launch.config_overrides)
    assert 'wire_api="responses"' in joined
    assert 'wire_api="chat"' not in joined
    assert 'base_url="https://openrouter.ai/api/v1"' in joined
    # Explicit model override wins over the (absent) provider default.
    assert launch.model == "qwen/q"


def test_resolve_native_codex_launch_subscription_logged_in_uses_cli_login(
    _isolated: Path,
) -> None:
    """A subscription default + a logged-in Codex → CLI login, openai pinned.

    When Codex actually has a stored login, deferring to its own auth is
    correct — the bridged ``auth.json`` authenticates it. The launch still
    pins the built-in ``openai`` provider: the bridged config.toml may set a
    custom default ``model_provider`` (e.g. isaac's Databricks AI Gateway),
    which would otherwise silently hijack the Subscription selection. Failure
    with extra overrides means we synthesized a provider route over a working
    subscription; failure with NO overrides means the pin regressed and a
    custom config.toml default can shadow the subscription again.
    """
    _seed(
        _isolated,
        {"codex-subscription": {"kind": "subscription", "cli": "codex", "default": True}},
    )
    _write_codex_login(_isolated, logged_in=True)

    launch = resolve_native_codex_launch(model=None)
    # Exactly the openai pin — no base_url/auth overrides (the login carries auth).
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None


def test_resolve_native_codex_launch_subscription_ignores_private_inherited_home(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    A private inherited ``CODEX_HOME`` does not hide the real Codex login.

    Nested Omnigent runs can inherit a per-session private Codex home from
    the parent native terminal. Subscription routing must check the same real
    ``~/.codex`` source that the app-server launch will bridge from; otherwise
    it falls through to a key provider even though the Codex CLI is logged in.

    :param _isolated: Isolated HOME/config directory.
    :param monkeypatch: Pytest fixture used to set inherited ``CODEX_HOME``.
    :returns: None.
    """
    _seed(
        _isolated,
        {
            "codex-subscription": {"kind": "subscription", "cli": "codex", "default": True},
            "openai": {
                "kind": "key",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-real",
                    "models": {"default": "gpt-5.5"},
                },
            },
        },
    )
    _write_codex_login(_isolated, logged_in=True)
    inherited = _isolated / ".omnigent" / "codex-native" / "abc123" / "codex-home"
    inherited.mkdir(parents=True)
    monkeypatch.setenv("CODEX_HOME", str(inherited))

    launch = resolve_native_codex_launch(model=None)

    # The openai pin (see the logged-in test); the point here is that no
    # key-provider overrides were synthesized despite the private CODEX_HOME.
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None


def test_resolve_native_codex_launch_subscription_no_login_falls_through_to_key(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription default but Codex NOT logged in → falls through to a real key.

    This is the core fix for the reported bug: a stale/dead subscription default
    must not strand the user at Codex's login screen when they have a real
    credential configured. The key is NOT the persisted default (the
    subscription is), so this proves the runtime fall-through, not a default
    change. Failure means the dead subscription shadows the key → empty
    overrides → Codex login prompt.
    """
    # No ambient providers, so the fall-through target is unambiguously the
    # explicitly-configured key (not a detected env key / Ollama).
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    _seed(
        _isolated,
        {
            "codex-subscription": {"kind": "subscription", "cli": "codex", "default": True},
            "openai": {
                "kind": "key",
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-real",
                    "models": {"default": "gpt-5.5"},
                },
            },
        },
    )
    _write_codex_login(_isolated, logged_in=False)

    launch = resolve_native_codex_launch(model=None)
    # Routed through the real key, not Codex's login.
    assert launch.profile is None
    assert launch.model == "gpt-5.5"
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert "printf %s sk-oai-real" in joined


def test_resolve_native_codex_launch_subscription_no_login_no_alternative_uses_login(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Subscription default, NOT logged in, no other provider → Codex login.

    With no usable Codex login and nothing to fall through to, dropping to
    Codex's own login is the correct outcome (the user must re-authenticate) —
    still pinned to the built-in ``openai`` provider so the login screen the
    user lands on is ChatGPT's, not a custom config.toml provider's. Failure
    with base_url/auth overrides would mean we fabricated a route from nothing.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    _seed(
        _isolated,
        {"codex-subscription": {"kind": "subscription", "cli": "codex", "default": True}},
    )
    _write_codex_login(_isolated, logged_in=False)

    launch = resolve_native_codex_launch(model=None)
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None


def test_resolve_native_codex_launch_databricks_provider_uses_profile(_isolated: Path) -> None:
    """A databricks provider default → the ucode profile path (its profile)."""
    _seed(
        _isolated,
        {"databricks": {"kind": "databricks", "default": True, "profile": "oss"}},
    )

    launch = resolve_native_codex_launch(model=None)
    assert launch.config_overrides == []
    # Routes via the Databricks profile path, not provider overrides.
    assert launch.profile == "oss"


def test_resolve_native_codex_launch_global_auth_when_no_provider(_isolated: Path) -> None:
    """No provider configured + a global Databricks ``auth:`` block → ucode.

    With the ``--profile`` flag removed, the global ``auth:`` block in
    ``config.yaml`` is the only spec-less way to route native Codex through a
    Databricks profile. Failure means the global auth fallback was skipped and
    the launch dropped to ambient detection / Codex's own login.
    """
    (_isolated / "config.yaml").write_text(
        yaml.safe_dump({"auth": {"type": "databricks", "profile": "oss"}})
    )

    launch = resolve_native_codex_launch(model=None)
    # Routes via the Databricks ucode profile path, not provider overrides.
    assert launch.config_overrides == []
    assert launch.profile == "oss"


def test_resolve_native_codex_launch_ambient_key_routes(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Spec-less with only an ambient OPENAI_API_KEY → provider overrides.

    First run without configure: native Codex still routes through the
    detected env key (api.openai.com), not the CLI login.
    """
    monkeypatch.setenv("OPENAI_API_KEY", "sk-oai-ambient")

    launch = resolve_native_codex_launch(model=None)
    assert launch.profile is None
    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://api.openai.com/v1"' in joined
    assert 'env_key="OPENAI_API_KEY"' in joined
    assert "sk-oai-ambient" not in joined
    assert launch.env_passthrough == ("OPENAI_API_KEY",)


def test_resolve_native_codex_launch_cli_config_default_pins_provider(
    _isolated: Path,
) -> None:
    """A cli-config default routes native codex via a model_provider pin only.

    The provider table + credential live in ~/.codex/config.toml (bridged
    into the session CODEX_HOME), so the launch must carry exactly the pin —
    no synthesized base_url/auth overrides, no profile, no forced model.
    Failure on the pin means an adopted isaac-style provider launches the
    native terminal on codex's built-in (unauthenticated) path; extra
    overrides mean we fabricated a transport over the config.toml one.
    """
    _seed(
        _isolated,
        {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
                "default": True,
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.profile is None
    assert launch.model is None


_DISMISSIBLE_CODEX_CONFIG = """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"

[model_providers.Databricks.auth]
command = "jq"
"""


def test_resolve_native_codex_launch_dismissed_config_provider_pins_openai(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Removed (dismissed) config.toml provider is neutralized at launch.

    With the detection dismissed and nothing else configured, the launch
    resolves NO provider — but the bridged ~/.codex/config.toml still sets
    ``model_provider = "Databricks"``, so an unpinned launch would silently
    route through the very credential the user removed (the reported bug:
    codex kept answering through the gateway after Remove). The launch must
    pin codex's built-in ``openai`` provider instead.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG)
    (_isolated / "config.yaml").write_text(
        yaml.safe_dump({"dismissed_detections": ["codex-databricks"]})
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None


def test_resolve_native_codex_launch_undismissed_config_provider_routes_via_pin(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The same config WITHOUT a dismissal routes through the detected provider.

    Counterpart to the dismissal test above: an isaac-configured machine
    that never Removed anything must keep routing through the gateway (via
    the detected cli-config provider's pin), not get force-pinned to
    ``openai``. Failure here means the no-provider neutralization fires too
    broadly and breaks the feature's golden path.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG)

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.profile is None


def test_config_provider_shadowed_by_nondefault_explicit_entry_still_pins(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A nondefault adopted entry cannot hide Codex's active config provider.

    The explicit entry shadows ambient synthesis by name, but Codex itself
    still selects the provider from config.toml. An empty launch would make a
    synthesized resume rollout record OpenAI and lose this provider's auth.
    """
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(_DISMISSIBLE_CODEX_CONFIG)
    _seed(
        _isolated,
        {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
            }
        },
    )

    launch = resolve_native_codex_launch(model="test-model")

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.model == "test-model"
    assert launch.profile is None


def test_shadowed_config_detection_uses_active_profile_provider(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fallback pins the provider selected by Codex's active profile."""
    monkeypatch.setattr("omnigent.onboarding.ambient._ollama_reachable", lambda: False)
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        'profile = "work"\n'
        'model_provider = "UnusedTopLevel"\n'
        "[profiles.work]\n"
        'model_provider = "Databricks"\n'
        "[model_providers.Databricks]\n"
        'name = "Databricks AI Gateway"\n'
        'base_url = "https://example.ai-gateway.cloud.databricks.com/codex/v1"\n'
        "[model_providers.Databricks.auth]\n"
        'command = "jq"\n'
    )
    _seed(
        _isolated,
        {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
                "display_name": "Databricks AI Gateway",
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides == ['model_provider="Databricks"']
    assert launch.profile is None


# ── Spec-level credentials (issue #2744) ────────────────────────────────────


def _spec(*, auth: ProviderAuth | None = None, profile: str | None = None) -> AgentSpec:
    """Build a minimal codex-native agent spec carrying spec-level credentials."""
    config: dict[str, object] = {"harness": "codex-native"}
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name="test-codex-native",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=None, auth=auth),
        llm=None,
        os_env=None,
    )


def test_spec_provider_auth_routes_when_machine_has_nothing(_isolated: Path) -> None:
    """A spec naming a provider routes natively with zero machine-level config.

    The #2744 repro: no machine provider, no global auth, codex not logged in.
    Pre-fix the launch fell through to "Codex CLI login" and the TUI parked on
    the sign-in screen; the spec's named provider must route instead.
    """
    _seed(
        _isolated,
        {
            "vendor-spec": {
                "kind": "key",
                "openai": {
                    "base_url": "https://spec.example.com/v1",
                    "api_key": "sk-spec",
                    "models": {"default": "spec-model"},
                },
            }
        },
    )

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="vendor-spec"))
    )

    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://spec.example.com/v1"' in joined
    assert "printf %s sk-spec" in joined
    assert launch.model == "spec-model"
    assert "Codex CLI login" not in launch.summary


def test_spec_provider_auth_beats_machine_default(_isolated: Path) -> None:
    """A spec-named provider wins over the machine-level default provider."""
    _seed(
        _isolated,
        {
            "machine-default": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://default.example.com/v1",
                    "api_key": "sk-default",
                },
            },
            "vendor-spec": {
                "kind": "key",
                "openai": {
                    "base_url": "https://spec.example.com/v1",
                    "api_key": "sk-spec",
                },
            },
        },
    )

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="vendor-spec"))
    )

    joined = "\n".join(launch.config_overrides)
    assert 'base_url="https://spec.example.com/v1"' in joined
    assert "default.example.com" not in joined


def test_spec_legacy_profile_routes_ucode(_isolated: Path) -> None:
    """A legacy ``executor.config.profile`` resolves to the ucode profile path."""
    launch = resolve_native_codex_launch(model=None, spec=_spec(profile="spec-prof"))

    assert launch.profile == "spec-prof"


def test_spec_without_auth_keeps_machine_resolution(_isolated: Path) -> None:
    """A spec with no spec-level credential leaves machine flows untouched."""
    _seed(
        _isolated,
        {
            "openai": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://api.openai.com/v1",
                    "api_key": "sk-oai-default",
                },
            }
        },
    )

    with_spec = resolve_native_codex_launch(model=None, spec=_spec())
    without_spec = resolve_native_codex_launch(model=None)

    assert with_spec == without_spec
    assert 'base_url="https://api.openai.com/v1"' in "\n".join(with_spec.config_overrides)


def test_spec_provider_auth_undeclared_fails_loud(_isolated: Path) -> None:
    """A spec naming an undeclared provider raises instead of a silent timeout."""
    with pytest.raises(OmnigentError, match="does-not-exist"):
        resolve_native_codex_launch(
            model=None, spec=_spec(auth=ProviderAuth(name="does-not-exist"))
        )


def _write_codex_home_login(root: Path, *, logged_in: bool) -> Path:
    """Write an explicit ``CODEX_HOME`` with a logged-in/out ``auth.json``."""
    codex_home = root / "codex-home"
    codex_home.mkdir(parents=True, exist_ok=True)
    content = '{"auth_mode": "apikey", "OPENAI_API_KEY": "sk-codex-login"}' if logged_in else "{}"
    (codex_home / "auth.json").write_text(content, encoding="utf-8")
    return codex_home


def test_spec_subscription_logged_out_does_not_substitute(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec-named subscription never silently routes a different provider.

    The machine-DEFAULT subscription path falls through to the first other
    routable provider when Codex is logged out (a safety net for defaults).
    For an explicit spec declaration that substitution would run the agent
    against a credential its author never named, so the launch must surface
    Codex's own login instead.
    """
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=False)))
    _seed(
        _isolated,
        {
            "codex-sub": {"kind": "subscription", "cli": "codex"},
            "other": {
                "kind": "key",
                "openai": {
                    "base_url": "https://other.example.com/v1",
                    "api_key": "sk-other",
                },
            },
        },
    )

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="codex-sub"))
    )

    assert launch.config_overrides == ['model_provider="openai"']
    assert "codex-sub" in launch.summary
    assert "other.example.com" not in "\n".join(launch.config_overrides)


def test_spec_subscription_logged_in_uses_cli_login(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec-named subscription with a live Codex login defers to it."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=True)))
    _seed(_isolated, {"codex-sub": {"kind": "subscription", "cli": "codex"}})

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="codex-sub"))
    )

    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.profile is None
    assert "codex-sub" in launch.summary
    assert "Codex is logged in" in launch.summary


@pytest.mark.parametrize("access_lane", ["omniroute", None])
def test_o3_launch_reuses_mcp_credential_without_global_env(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch, access_lane: str | None
) -> None:
    monkeypatch.delenv("OMNIROUTE_O3_KEY", raising=False)
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    codex_dir = _isolated / ".codex"
    codex_dir.mkdir()
    (codex_dir / "config.toml").write_text(
        '[mcp_servers.omniroute]\nurl = "http://127.0.0.1:20128/mcp"\n'
        '[mcp_servers.omniroute.http_headers]\nAuthorization = "Bearer local-sentinel"\n'
    )
    launch = resolve_native_codex_launch(model="custom/o3-route-deadbeef", access_lane=access_lane)
    assert launch.credential_env == {"OMNIROUTE_O3_KEY": "local-sentinel"}
    assert "local-sentinel" not in repr(launch)
    assert "local-sentinel" not in "\n".join(launch.config_overrides)
    assert "OMNIROUTE_O3_KEY" not in os.environ
    assert launch.trace_provenance.access_lane == "omniroute"


def test_o3_auxiliary_launch_does_not_fall_back_to_subscription(_isolated: Path) -> None:
    _write_codex_login(_isolated, logged_in=True)
    with pytest.raises(OmnigentError, match="OmniRoute lane unavailable"):
        resolve_native_codex_launch(model="custom/o3-route-deadbeef")


# ── login_required: headless fail-fast marker ──────────────────────────────


def test_no_provider_and_no_codex_login_marks_login_required(_isolated: Path) -> None:
    """No provider + no Codex login → the launch is marked ``login_required``.

    This is the routing state in which a headless launch parks the TUI on the
    sign-in screen forever; the flag lets the runner fail chat turns fast
    instead of burning the thread-start timeout.
    """
    launch = resolve_native_codex_launch(model=None)

    assert launch.profile is None
    assert "no provider configured" in launch.summary
    assert launch.login_required is True


def test_no_provider_but_codex_logged_in_is_not_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No provider but a live Codex login → the TUI starts fine, no fail-fast."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=True)))

    launch = resolve_native_codex_launch(model=None)

    assert "Codex CLI login" in launch.summary
    assert launch.login_required is False


def test_routable_provider_is_not_login_required(_isolated: Path) -> None:
    """A provider that routes Codex never sets ``login_required``."""
    _seed(
        _isolated,
        {
            "vendor": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://vendor.example.com/v1",
                    "api_key": "sk-vendor",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)

    assert launch.config_overrides
    assert launch.login_required is False


def test_subscription_default_logged_out_no_fallback_marks_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A logged-out subscription default with nothing to fall through to is doomed headlessly."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=False)))
    _seed(_isolated, {"codex-sub": {"kind": "subscription", "cli": "codex", "default": True}})

    launch = resolve_native_codex_launch(model=None)

    assert "has no usable Codex login" in launch.summary
    assert launch.login_required is True


def test_subscription_default_logged_in_is_not_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A subscription default with a live Codex login starts fine — no fail-fast."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=True)))
    _seed(_isolated, {"codex-sub": {"kind": "subscription", "cli": "codex", "default": True}})

    launch = resolve_native_codex_launch(model=None)

    assert "Codex is logged in" in launch.summary
    assert launch.login_required is False


def test_spec_subscription_logged_out_marks_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec-named subscription on a logged-out Codex is doomed headlessly."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=False)))
    _seed(_isolated, {"codex-sub": {"kind": "subscription", "cli": "codex"}})

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="codex-sub"))
    )

    assert launch.login_required is True


def test_spec_subscription_logged_in_is_not_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A spec-named subscription with a live Codex login needs no fail-fast."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=True)))
    _seed(_isolated, {"codex-sub": {"kind": "subscription", "cli": "codex"}})

    launch = resolve_native_codex_launch(
        model=None, spec=_spec(auth=ProviderAuth(name="codex-sub"))
    )

    assert launch.login_required is False


def test_default_provider_without_credential_logged_out_marks_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A default provider with no usable openai credential falls to a doomed login."""
    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=False)))
    monkeypatch.delenv("MISSING_CODEX_TEST_KEY", raising=False)
    _seed(
        _isolated,
        {
            "broken": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": "https://broken.example.com/v1",
                    # A credential reference that cannot resolve in this
                    # process: the provider parses but cannot route.
                    "api_key_ref": "env:MISSING_CODEX_TEST_KEY",
                },
            }
        },
    )

    launch = resolve_native_codex_launch(model=None)

    assert "no usable openai credential" in launch.summary
    assert launch.login_required is True


def test_global_auth_block_login_logged_out_marks_login_required(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-Databricks global auth block falling to Codex login is doomed headlessly."""
    from omnigent.runtime import workflow

    monkeypatch.setenv("CODEX_HOME", str(_write_codex_home_login(_isolated, logged_in=False)))
    monkeypatch.setattr(workflow, "_load_global_auth", lambda: object())

    launch = resolve_native_codex_launch(model=None)

    assert "global auth block" in launch.summary
    assert launch.login_required is True


def test_explicit_glm_direct_lane_requires_zai_credential(_isolated: Path) -> None:
    """A missing Z.ai credential fails the lane explicitly, never via OmniRoute."""
    with pytest.raises(OmnigentError, match=r"^Z\.AI Direct is not configured"):
        resolve_native_codex_launch(model="glm-5.3", access_lane="glm-direct")


def test_explicit_glm_direct_lane_uses_native_zai_provider(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct lane registers a native Responses provider at Z.ai's /api/v1."""
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")

    launch = resolve_native_codex_launch(model="glm-5.3", access_lane="glm-direct")

    joined = "\n".join(launch.config_overrides)
    assert launch.model == "glm-5.3"
    assert launch.profile is None
    assert 'base_url="https://api.z.ai/api/v1"' in joined
    assert 'env_key="ZAI_API_KEY"' in joined
    assert 'wire_api="responses"' in joined
    assert launch.env_passthrough == ("ZAI_API_KEY",)
    assert launch.credential_env == {"ZAI_API_KEY": "zai-test-placeholder"}
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "glm-direct"
    assert launch.trace_provenance.provider == "z.ai"
    assert launch.trace_provenance.provider_fallback is False


def test_explicit_glm_direct_lane_accepts_omniroute_spelling(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The route spelling resolves to the provider-local id on the direct lane."""
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")

    launch = resolve_native_codex_launch(model="glm/glm-5.3-flash", access_lane="glm-direct")

    assert launch.model == "glm-5.3-flash"
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "glm-direct"


def test_explicit_glm_direct_lane_rejects_unlisted_model(_isolated: Path) -> None:
    """Models the direct provider does not serve fail closed on the direct lane."""
    with pytest.raises(OmnigentError, match=r"^Z\.AI Direct does not serve model"):
        resolve_native_codex_launch(model="glm/glm-4.6", access_lane="glm-direct")


def test_glm_direct_lane_never_routes_through_omniroute(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A selected direct lane ignores a fully configured OmniRoute environment."""
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "omniroute-test-placeholder")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    launch = resolve_native_codex_launch(model="glm-5.3", access_lane="glm-direct")

    joined = "\n".join(launch.config_overrides)
    assert "api.z.ai" in joined
    assert "127.0.0.1:20128" not in joined
    assert "OMNIROUTE_O3_KEY" not in joined
    assert launch.env_passthrough == ("ZAI_API_KEY",)


def test_omniroute_lane_for_glm_never_consumes_zai_direct(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The OmniRoute lane keeps routing GLM through the gateway, not Z.ai."""
    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "omniroute-test-placeholder")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    launch = resolve_native_codex_launch(model="glm/glm-5.3", access_lane="omniroute")

    joined = "\n".join(launch.config_overrides)
    assert 'base_url="http://127.0.0.1:20128/v1"' in joined
    assert "api.z.ai" not in joined
    assert "ZAI_API_KEY" not in joined
    assert launch.env_passthrough == ("OMNIROUTE_O3_KEY",)
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "omniroute"


def test_zai_direct_discovery_offers_exactly_the_served_models(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Discovery mirrors the direct provider's catalogue and never invents rows."""
    import json as _json
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")
    app_server._zai_direct_model_cache.clear()

    payload = {
        "models": [
            {
                "slug": "glm-5.3",
                "default_reasoning_level": "max",
                "supported_reasoning_levels": [
                    {"effort": "low", "description": "Fast"},
                    {"effort": "high", "description": "Deep"},
                    {"effort": "max", "description": "Deepest"},
                ],
            },
            {"slug": "glm-5.3-flash", "default_reasoning_level": "max"},
            # Not a direct-lane model: must never surface as a direct row.
            {"slug": "glm-4.6"},
            {"slug": "some-other-provider-model"},
        ]
    }

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            return _json.dumps(payload).encode()

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> _Response:
        assert request.full_url == "https://api.z.ai/api/v1/models"
        assert timeout == 10
        return _Response()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    try:
        rows = app_server.zai_direct_glm_catalog_rows()
    finally:
        app_server._zai_direct_model_cache.clear()

    assert [row["id"] for row in rows] == ["glm-5.3", "glm-5.3-flash"]
    assert [row["displayName"] for row in rows] == ["GLM 5.3", "GLM 5.3 Flash"]
    assert rows[0]["defaultReasoningEffort"] == "max"
    assert [level["reasoningEffort"] for level in rows[0]["supportedReasoningEfforts"]] == [
        "low",
        "high",
        "max",
    ]
    # glm-5.3-flash carries no levels in the fixture: none are invented.
    assert "supportedReasoningEfforts" not in rows[1]


def test_zai_direct_discovery_requires_credential(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No key means no direct rows; the lane never leaks into other lanes."""
    from omnigent.harnesses.codex_native import app_server

    app_server._zai_direct_model_cache.clear()
    assert app_server.zai_direct_glm_catalog_rows() == ()


def test_glm_lane_vocabulary_keeps_direct_eligibility_narrow() -> None:
    """Gateway route knowledge is a superset of Direct eligibility, by design.

    Every direct model must stay reachable from a gateway route (an asymmetric
    mapped pair is what silently stranded the direct lane behind PR #171), but
    the gateway vocabulary also carries OmniRoute-only generations: knowing a
    route for display/qualification on the gateway lane never implies the
    direct provider serves it.
    """
    from omnigent.models.glm_model_vocabulary import (
        GLM_DIRECT_MODELS,
        GLM_OMNIROUTE_ROUTES,
        GLM_OMNIROUTE_TO_DIRECT,
    )

    # Direct eligibility stays exactly the served pair.
    assert {"glm-5.3", "glm-5.3-flash"} == GLM_DIRECT_MODELS
    # Every direct model is reachable from a route, under the local spelling.
    assert set(GLM_OMNIROUTE_TO_DIRECT.values()) == GLM_DIRECT_MODELS
    # Gateway route knowledge is a strict superset: OmniRoute-only
    # generations are known for the gateway lane and mapped to nothing.
    assert set(GLM_OMNIROUTE_TO_DIRECT) < set(GLM_OMNIROUTE_ROUTES)
    assert GLM_OMNIROUTE_ROUTES["glm/glm-4.6"] is None
    for route_id, direct_id in GLM_OMNIROUTE_ROUTES.items():
        assert route_id.startswith("glm/")
        if direct_id is not None:
            assert route_id.split("/", 1)[1] == direct_id
            assert direct_id in GLM_DIRECT_MODELS


def test_lane_launch_guard_holds_for_every_lane(_isolated: Path) -> None:
    """The production launch resolver rejects model=None on any lane.

    Catalogue probes fingerprint a model-free launch SHAPE, which they must
    obtain through :func:`resolve_native_codex_catalog_launch` — never by
    calling the launch resolver with the invalid model=None+lane shape this
    guard deliberately rejects (the exception-swallowing skip that audit item
    D reproduced).
    """
    for lane in ("omniroute", "codex-direct", "glm-direct"):
        with pytest.raises(OmnigentError, match="requires an explicit model"):
            resolve_native_codex_launch(model=None, access_lane=lane)


def test_catalog_launch_resolver_gives_the_direct_lane_a_model_free_shape(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The direct lane's catalogue shape carries the provider, never a pin."""
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    monkeypatch.setenv("ZAI_API_KEY", "zai-test-placeholder")

    launch = resolve_native_codex_catalog_launch(access_lane="glm-direct")

    joined = "\n".join(launch.config_overrides)
    assert launch.model is None
    assert launch.profile is None
    assert 'base_url="https://api.z.ai/api/v1"' in joined
    assert 'env_key="ZAI_API_KEY"' in joined
    assert 'wire_api="responses"' in joined
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "glm-direct"
    assert launch.trace_provenance.provider == "z.ai"
    assert launch.trace_provenance.provider_fallback is False
    # Secret-safe: the credential rides the env passthrough, never the shape.
    assert launch.env_passthrough == ("ZAI_API_KEY",)
    assert launch.credential_env == {"ZAI_API_KEY": "zai-test-placeholder"}
    assert "zai-test-placeholder" not in joined
    assert "zai-test-placeholder" not in launch.summary


def test_catalog_launch_resolver_requires_the_lane_credential(_isolated: Path) -> None:
    """An unconfigured lane fails its catalogue shape explicitly."""
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    with pytest.raises(OmnigentError, match=r"^Z\.AI Direct is not configured"):
        resolve_native_codex_catalog_launch(access_lane="glm-direct")


def test_catalog_launch_resolver_gives_omniroute_a_model_free_shape(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gateway catalogue shape pins the gateway, not a session model."""
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    monkeypatch.setenv("OMNIROUTE_O3_KEY", "omniroute-test-placeholder")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    launch = resolve_native_codex_catalog_launch(access_lane="omniroute")

    joined = "\n".join(launch.config_overrides)
    assert launch.model is None
    assert 'base_url="http://127.0.0.1:20128/v1"' in joined
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "omniroute"
    # Secret-safe: the token rides the env passthrough, never the overrides.
    assert launch.env_passthrough == ("OMNIROUTE_O3_KEY",)
    assert "omniroute-test-placeholder" not in joined
    assert "omniroute-test-placeholder" not in launch.summary


def test_catalog_launch_resolver_gives_codex_direct_a_model_free_shape(
    _isolated: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The subscription catalogue shape is Codex's own login, model-free."""
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    monkeypatch.setattr(
        "omnigent.onboarding.ambient.codex_auth_has_credential", lambda _path: True
    )

    launch = resolve_native_codex_catalog_launch(access_lane="codex-direct")

    assert launch.model is None
    assert launch.config_overrides == ['model_provider="openai"']
    assert launch.trace_provenance is not None
    assert launch.trace_provenance.access_lane == "codex-direct"
    assert launch.trace_provenance.provider_fallback is False


def test_catalog_launch_resolver_with_no_lane_uses_the_default_shape(
    _isolated: Path,
) -> None:
    """Without a lane the catalogue shape is the plain default resolution."""
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    _seed(
        _isolated,
        {
            "production-gateway": {
                "kind": "gateway",
                "default": True,
                "openai": {
                    "base_url": "https://gateway.example.test/v1",
                    "api_key": "test-placeholder",
                },
            }
        },
    )

    launch = resolve_native_codex_catalog_launch()

    assert launch.model is None
    assert "gateway.example.test" in "\n".join(launch.config_overrides)


def test_catalog_launch_resolver_rejects_unknown_lane(_isolated: Path) -> None:
    from omnigent.harnesses.codex_native.app_server import resolve_native_codex_catalog_launch

    with pytest.raises(OmnigentError, match="Unsupported native Codex access lane"):
        resolve_native_codex_catalog_launch(access_lane="bogus-lane")


# ---------------------------------------------------------------------------
# GLM catalogue cache identity (audit item C): entries are scoped to the
# connection that produced them — lane, endpoint, and a non-secret digest of
# the credential/configuration — never to bare lane constants.
# ---------------------------------------------------------------------------


def _zai_http_payload(payload: dict[str, object]) -> object:
    """A context-manager stand-in for urlopen() serving one JSON payload."""

    class _Response:
        def __enter__(self) -> _Response:
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def read(self) -> bytes:
            import json as _json

            return _json.dumps(payload).encode()

    return _Response()


@pytest.fixture()
def _glm_cache_isolated(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fresh GLM caches and a clean env for every cache test."""
    from omnigent.harnesses.codex_native import app_server

    app_server._zai_direct_model_cache.clear()
    app_server._omniroute_glm_model_cache.clear()
    monkeypatch.delenv("ZAI_API_KEY", raising=False)
    monkeypatch.delenv("OMNIROUTE_O3_KEY", raising=False)
    yield
    app_server._zai_direct_model_cache.clear()
    app_server._omniroute_glm_model_cache.clear()


def test_zai_direct_cache_is_scoped_to_the_credential(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A replaced account key re-fetches: account A's rows never serve B."""
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    fetches: list[str] = []
    payloads = (
        {"models": [{"slug": "glm-5.3"}]},
        {"models": [{"slug": "glm-5.3-flash"}]},
    )

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        fetches.append(str(request.get_header("Authorization")))
        return _zai_http_payload(payloads[len(fetches) - 1])

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    monkeypatch.setenv("ZAI_API_KEY", "key-account-A")
    rows_a = app_server.zai_direct_glm_catalog_rows()
    assert [row["id"] for row in rows_a] == ["glm-5.3"]
    # Warm cache: the same credential is served without another fetch.
    assert app_server.zai_direct_glm_catalog_rows() == rows_a
    assert len(fetches) == 1

    monkeypatch.setenv("ZAI_API_KEY", "key-account-B")
    rows_b = app_server.zai_direct_glm_catalog_rows()
    assert [row["id"] for row in rows_b] == ["glm-5.3-flash"]
    assert len(fetches) == 2, "a new credential must not replay account A's rows"


def test_zai_direct_cache_hides_rows_without_a_key(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Removing the key hides the lane even with a warm cache, fetching nothing."""
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        return _zai_http_payload({"models": [{"slug": "glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ZAI_API_KEY", "key-account-A")
    assert app_server.zai_direct_glm_catalog_rows()

    monkeypatch.delenv("ZAI_API_KEY")
    assert app_server.zai_direct_glm_catalog_rows() == ()


def test_zai_direct_cache_refreshes_after_expiry(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An expired TTL entry re-fetches instead of serving stale rows."""
    import urllib.request

    from cachetools import TTLCache

    from omnigent.harnesses.codex_native import app_server

    fetches: list[int] = []

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        fetches.append(len(fetches))
        return _zai_http_payload({"models": [{"slug": "glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ZAI_API_KEY", "key-account-A")
    monkeypatch.setattr(app_server, "_zai_direct_model_cache", TTLCache(maxsize=8, ttl=0.05))

    app_server.zai_direct_glm_catalog_rows()
    app_server.zai_direct_glm_catalog_rows()
    assert len(fetches) == 1
    time.sleep(0.08)
    app_server.zai_direct_glm_catalog_rows()
    assert len(fetches) == 2, "an expired entry must refresh from the provider"


def test_zai_direct_cache_single_flights_concurrent_access(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Racing readers on a cold cache pay exactly one provider fetch."""
    import threading
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    fetch_lock = threading.Lock()
    fetches: list[int] = []
    first_arrived = threading.Event()
    release = threading.Event()

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        with fetch_lock:
            fetches.append(1)
        first_arrived.set()
        release.wait(timeout=5)
        return _zai_http_payload({"models": [{"slug": "glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ZAI_API_KEY", "key-account-A")

    results: list[tuple[object, ...]] = []
    errors: list[BaseException] = []

    def reader() -> None:
        try:
            results.append(app_server.zai_direct_glm_catalog_rows())
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=reader) for _ in range(8)]
    for thread in threads:
        thread.start()
    # Deterministic single-flight: once the one fetch has started, every other
    # reader either blocks on the cache lock or arrives after the cache is
    # populated — either way exactly one provider fetch may happen.
    assert first_arrived.wait(timeout=5)
    release.set()
    for thread in threads:
        thread.join(timeout=5)

    assert errors == []
    assert len(fetches) == 1, "concurrent readers must single-flight one fetch"
    assert all(rows == results[0] for rows in results)


def test_zai_direct_and_omniroute_cache_keys_never_carry_credentials(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Cache keys hold credential digests, never raw secrets."""
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        return _zai_http_payload({"models": [{"slug": "glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("ZAI_API_KEY", "zai-secret-do-not-leak")
    app_server.zai_direct_glm_catalog_rows()

    keys = list(app_server._zai_direct_model_cache.keys())
    assert keys, "the direct catalogue must have been cached"
    assert all("zai-secret-do-not-leak" not in key for key in keys)

    # The OmniRoute cache shares the convention.
    def omni_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        return _zai_http_payload({"data": [{"id": "glm/glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", omni_urlopen)
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "omniroute-secret-do-not-leak")
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")
    launch = app_server.resolve_native_codex_catalog_launch(access_lane="omniroute")
    monkeypatch.setattr(app_server, "resolve_native_codex_launch", lambda *, model: launch)
    app_server.omniroute_glm_catalog_rows()

    omni_keys = list(app_server._omniroute_glm_model_cache.keys())
    assert omni_keys, "the gateway catalogue must have been cached"
    assert all("omniroute-secret-do-not-leak" not in key for key in omni_keys)
    # And the digest is stable: the same credential maps to the same key.
    assert app_server._omniroute_cache_key(
        "http://x/v1", "tok"
    ) == app_server._omniroute_cache_key("http://x/v1", "tok")
    assert app_server._omniroute_cache_key(
        "http://x/v1", "tok1"
    ) != app_server._omniroute_cache_key("http://x/v1", "tok2")


def test_omniroute_cache_is_resolved_against_the_current_configuration(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Endpoint/credential changes re-resolve before the cache is consulted."""
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    fetches: list[str] = []

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        auth = str(request.get_header("Authorization"))
        fetches.append(auth)
        return _zai_http_payload({"data": [{"id": "glm/glm-5.3"}]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20128")

    def launch_for_base(base: str) -> app_server.NativeCodexLaunch:
        return app_server.NativeCodexLaunch(
            config_overrides=app_server._provider_codex_config_overrides(
                model=None,
                base_url=f"{base}/v1",
                env_key="OMNIROUTE_O3_KEY",
                wire_api="responses",
            ),
            model=None,
            profile=None,
            summary="omniroute-catalogue",
            env_passthrough=("OMNIROUTE_O3_KEY",),
        )

    monkeypatch.setenv("OMNIROUTE_O3_KEY", "token-one")
    monkeypatch.setattr(
        app_server,
        "resolve_native_codex_launch",
        lambda *, model: launch_for_base("http://127.0.0.1:20128"),
    )
    rows_one = app_server.omniroute_glm_catalog_rows()
    assert rows_one
    assert app_server.omniroute_glm_catalog_rows() == rows_one
    assert len(fetches) == 1, "a warm, matching connection is served from cache"

    # Same base URL, rotated credential: the token is re-read BEFORE the cache
    # lookup, so the new connection re-fetches.
    monkeypatch.setenv("OMNIROUTE_O3_KEY", "token-two")
    app_server.omniroute_glm_catalog_rows()
    assert len(fetches) == 2
    assert fetches[-1].endswith("token-two")

    # Changed gateway endpoint (a different provider/config generation):
    # the current configuration is resolved again, not replayed from cache.
    monkeypatch.setattr(
        app_server,
        "resolve_native_codex_launch",
        lambda *, model: launch_for_base("http://127.0.0.1:20199"),
    )
    monkeypatch.setenv("OMNIGENT_O3_OMNIROUTE_BASE_URL", "http://127.0.0.1:20199")
    app_server.omniroute_glm_catalog_rows()
    assert len(fetches) == 3, "an endpoint change must not serve the old gateway's rows"


def test_omniroute_rows_qualify_gateway_only_generations(
    _isolated: Path, _glm_cache_isolated: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Gateway route knowledge, not Direct eligibility, filters OmniRoute rows.

    An OmniRoute-only generation (e.g. glm-4.6) qualifies on the gateway lane
    because the gateway serves it — without implying any Direct support.
    """
    import urllib.request

    from omnigent.harnesses.codex_native import app_server

    def fake_urlopen(request: urllib.request.Request, *, timeout: float) -> object:
        return _zai_http_payload(
            {
                "data": [
                    {"id": "glm/glm-5.3", "effort_tiers": ["high"]},
                    {"id": "glm/glm-4.6"},
                    {"id": "gpt-5.6"},
                ]
            }
        )

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    rows = app_server._omniroute_glm_rows_uncached("http://127.0.0.1:20128/v1", "tok")

    assert [row["id"] for row in rows] == ["glm/glm-4.6", "glm/glm-5.3"]
    by_id = {row["id"]: row for row in rows}
    assert by_id["glm/glm-4.6"]["displayName"] == "GLM 4.6"
    assert by_id["glm/glm-5.3"]["displayName"] == "GLM 5.3"
    assert by_id["glm/glm-5.3"]["supportedReasoningEfforts"] == [{"reasoningEffort": "high"}]
    # ...while the direct lane still refuses the gateway-only generation.
    with pytest.raises(OmnigentError, match=r"^Z\.AI Direct does not serve model"):
        resolve_native_codex_launch(model="glm/glm-4.6", access_lane="glm-direct")
