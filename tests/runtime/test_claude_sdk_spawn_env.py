"""
Tests for ``_build_claude_sdk_spawn_env`` in
``omnigent/runtime/workflow.py``.

The spawn-env builder maps ``spec.executor`` fields to
``HARNESS_CLAUDE_SDK_*`` env vars that the claude-sdk harness wrap reads
at executor-construction time.  Mirrors the pattern of
``test_openai_agents_sdk_spawn_env.py`` for the openai-agents harness.

This is a unit test — no subprocess spawn, no real claude CLI.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml as _yaml

from omnigent.runtime.workflow import _build_claude_sdk_spawn_env
from omnigent.spec.types import (
    AgentSpec,
    ApiKeyAuth,
    DatabricksAuth,
    ExecutorSpec,
    LLMConfig,
)


@pytest.fixture(autouse=True)
def _isolate_global_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Point OMNIGENT_CONFIG_HOME at an empty temp dir for every test in
    this file so tests that don't explicitly set up a global config are
    not affected by the developer's real ``~/.omnigent/config.yaml``.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Temporary directory for the isolated config.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))


def _make_spec(
    *,
    model: str | None = "databricks-claude-sonnet-4-6",
    profile: str | None = None,
    auth: ApiKeyAuth | DatabricksAuth | None = None,
) -> AgentSpec:
    """
    Build a minimal claude-sdk :class:`AgentSpec` for spawn-env tests.

    :param model: Model identifier threaded into executor config and
        ``spec.llm``, e.g. ``"databricks-claude-sonnet-4-6"``.
    :param profile: Legacy profile set via ``executor.config["profile"]``.
        ``None`` omits it (no profile declared in YAML).
    :param auth: Typed auth object placed on ``spec.executor.auth``.
        ``None`` omits it (harness falls back to legacy / global config).
    :returns: A populated :class:`AgentSpec`.
    """
    config: dict[str, object] = {"harness": "claude-sdk"}
    if model is not None:
        config["model"] = model
    if profile is not None:
        config["profile"] = profile
    return AgentSpec(
        spec_version=1,
        name="test-claude-sdk",
        instructions="You are a test agent.",
        executor=ExecutorSpec(type="omnigent", config=config, model=model, auth=auth),
        llm=LLMConfig(model=model) if model is not None else None,
    )


def test_databricks_auth_sets_databricks_env_vars() -> None:
    """
    ``executor.auth: {type: databricks, profile: …}`` sets
    ``HARNESS_CLAUDE_SDK_GATEWAY=true`` and
    ``HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE``.

    Failure means a spec that explicitly declares Databricks auth still
    gets routed to api.anthropic.com and fails with "model not found".
    """
    spec = _make_spec(auth=DatabricksAuth(profile="my-profile"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    assert env["HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE"] == "my-profile"


def test_api_key_auth_sets_helper_env_var() -> None:
    """
    ``executor.auth: {type: api_key, api_key: …}`` sets
    ``HARNESS_CLAUDE_SDK_API_KEY_HELPER`` to a printf shell command.

    Failure means the API key never reaches the Claude CLI's
    ``settings.apiKeyHelper`` and the agent falls back to subscription
    auth silently.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-ant-test-123"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" in env
    # The helper command must echo the literal key (shlex-quoted for safety).
    assert "sk-ant-test-123" in env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # api_key auth does not trigger Databricks routing.
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env


def test_api_key_auth_with_special_chars_is_shell_safe() -> None:
    """
    API keys containing shell-special characters (spaces, quotes, ``$``)
    are safely quoted in the helper command via ``shlex.quote``.

    Failure means a key like ``sk-$weird`` could be misinterpreted by
    the shell when the Claude CLI invokes the helper command.
    """
    spec = _make_spec(model=None, auth=ApiKeyAuth(api_key="sk-$weird 'key'"))
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    helper = env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"]
    # The raw key must NOT appear unquoted.
    assert "sk-$weird 'key'" not in helper
    # shlex-quoted form must be present.
    assert "sk-" in helper


def test_global_config_databricks_auth_applied_when_spec_has_no_auth(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    When the spec declares no auth, ``_load_global_auth()`` is consulted
    and a global ``auth: {type: databricks, profile: …}`` is applied.

    Failure means ``omnigent setup`` auth configuration is silently
    ignored for claude-sdk agents (it was applied to openai-agents but
    not claude-sdk before this fix).
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_yaml.dump({"auth": {"type": "databricks", "profile": "global-profile"}}))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    spec = _make_spec(auth=None, profile=None)
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true"
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "global-profile"


def test_global_config_not_applied_when_spec_has_legacy_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """
    When the spec uses a legacy ``executor.config["profile"]``, the global
    config ``auth:`` block is not applied — spec-level auth always wins.

    Failure means a YAML with ``executor.profile: oss`` gets silently
    overridden by the user's global api_key config.
    """
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(_yaml.dump({"auth": {"type": "api_key", "api_key": "sk-global"}}))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))

    spec = _make_spec(auth=None, profile="oss-from-spec")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Legacy profile must be used; global api_key must not interfere.
    assert env.get("HARNESS_CLAUDE_SDK_GATEWAY") == "true"
    assert env.get("HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE") == "oss-from-spec"
    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" not in env


def _ucode_state_without_model(monkeypatch: pytest.MonkeyPatch, *, model: str | None):
    """
    Mock ucode resolution to a claude agent with the given model.

    Builds a workspace state whose ``claude`` agent carries a gateway URL +
    auth command but ``model=model`` and no ``claude_models`` tiers, then
    monkeypatches the workflow module's ucode lookups to return it.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param model: Per-agent ucode model, e.g. ``None`` to simulate a
        workspace that caches no model, or ``"databricks-claude-sonnet-4-6"``.
    """
    from omnigent.onboarding.ucode_state import UcodeAgentState, UcodeWorkspaceState

    state = UcodeWorkspaceState(
        workspace_url="https://example.databricks.com",
        claude_models={},
        agents={
            "claude": UcodeAgentState(
                model=model,
                base_url="https://example.databricks.com/ai-gateway/anthropic",
                auth_command="printf token",
            )
        },
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.get_workspace_url_for_profile",
        lambda profile: "https://example.databricks.com",
    )
    monkeypatch.setattr(
        "omnigent.runtime.workflow.read_ucode_state",
        lambda workspace_url: state,
    )


def test_ucode_state_without_model_falls_back_to_databricks_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A modelless ucode state resolves the Databricks gateway default model.

    Reproduces the nessie failure: a profile-backed claude-sdk agent with no
    spec model, whose workspace ucode state caches a gateway URL but no model.
    Without the producer default the CLI falls back to its host-config model
    (an Anthropic-direct id the gateway rejects), so the model env var must be
    set to a routable ``databricks-*`` endpoint name.
    """
    _ucode_state_without_model(monkeypatch, model=None)

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    # The verified routable gateway endpoint name, not the CLI's own default.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-opus-4-8"


def test_ucode_state_with_model_is_not_overridden_by_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A ucode-supplied model is used as-is; the default does not clobber it.

    Failure means the producer's missing-model fallback would override a
    workspace that correctly caches its own model.
    """
    _ucode_state_without_model(monkeypatch, model="databricks-claude-sonnet-4-6")

    spec = _make_spec(model=None, profile="oss")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "databricks-claude-sonnet-4-6"


# ── Custom-endpoint gateway plumbing (Control Room V2) ─────────────────────


def test_api_key_auth_with_base_url_enables_gateway_and_threads_base_url() -> None:
    """
    ``executor.auth: {type: api_key, api_key: …, base_url: …}`` enables
    the Claude SDK gateway transport AND threads the base URL.

    This is the new path the Control Room V2 top-level uses: the
    orchestrator's ``executor.auth.base_url`` points at OmniRoute's local
    Anthropic Messages endpoint so the SDK subprocess dials it instead of
    api.anthropic.com. Failure means the SDK would fall through to
    api.anthropic.com and abort on a missing subscription auth, or it
    would dial a wrong endpoint — exactly the regression the
    ``harness: claude-sdk`` swap was supposed to fix.
    """
    spec = _make_spec(
        model="custom/best-coding",
        auth=ApiKeyAuth(
            api_key="sk-omniroute-test",
            base_url="http://127.0.0.1:20128/v1",
        ),
    )
    env = _build_claude_sdk_spawn_env(spec, workdir=None)

    # Gateway transport is enabled.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY"] == "true"
    # Base URL is threaded verbatim.
    assert env["HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL"] == "http://127.0.0.1:20128/v1"
    # Auth command is a printf shell wrapper around the bearer token
    # (NEVER an inline bearer string in the env — that would leak the
    # secret through the spawn-env dict and into the SDK subprocess).
    auth_cmd = env["HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND"]
    assert auth_cmd.startswith("printf %s "), (
        f"gateway auth command must be a printf shell wrapper (got {auth_cmd!r})"
    )
    assert "sk-omniroute-test" in auth_cmd
    # apiKeyHelper mirrors the auth command (the SDK uses the helper to
    # refresh long sessions, so it must point at the same bearer source).
    assert env["HARNESS_CLAUDE_SDK_API_KEY_HELPER"] == auth_cmd
    # The model passed in the spec survives into HARNESS_CLAUDE_SDK_MODEL.
    assert env["HARNESS_CLAUDE_SDK_MODEL"] == "custom/best-coding"


def test_api_key_auth_with_base_url_does_not_leak_inline_bearer() -> None:
    """
    The custom-endpoint gateway plumbing must never inline the bearer
    token in any non-shell-wrapper env value.

    The brief is explicit: do not embed secrets. The
    ``HARNESS_CLAUDE_SDK_*`` env dict is the spawn-env the runner hands
    the SDK subprocess, so a literal bearer there would be a leak. The
    printf shell wrapper is the only acceptable form.
    """
    bearer = "sk-very-secret-key-1234567890"
    spec = _make_spec(
        model="custom/best-coding",
        auth=ApiKeyAuth(api_key=bearer, base_url="http://127.0.0.1:20128/v1"),
    )
    env = _build_claude_sdk_spawn_env(spec, workdir=None)
    # Bearer must appear ONLY inside the printf shell wrapper — never as
    # a standalone value in any other env key.
    for key, value in env.items():
        if key in (
            "HARNESS_CLAUDE_SDK_API_KEY_HELPER",
            "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND",
        ):
            # These are allowed to contain the bearer because they're
            # printf shell wrappers that the SDK CLI invokes.
            assert bearer in value
            continue
        assert bearer not in value, (
            f"bearer token leaked into non-shell-wrapper env {key!r} (value={value!r})"
        )


def test_api_key_auth_without_base_url_does_not_enable_gateway() -> None:
    """
    An ``api_key`` auth WITHOUT a ``base_url`` must not enable the
    gateway transport.

    The gateway is for routing through a custom endpoint. An api_key
    without a base_url means "use Anthropic's default endpoint with this
    key" — which is api.anthropic.com territory and out-of-scope for
    Control Room V2. Failure here means Control Room would enable the
    gateway flag and re-write the base URL even when no custom URL is
    declared, dialing an empty endpoint.
    """
    spec = _make_spec(
        model="custom/best-coding",
        auth=ApiKeyAuth(api_key="sk-test-no-base"),
    )
    env = _build_claude_sdk_spawn_env(spec, workdir=None)
    assert "HARNESS_CLAUDE_SDK_GATEWAY" not in env
    assert "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL" not in env
    assert "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND" not in env
    # apiKeyHelper is still set (it always is for ApiKeyAuth with a key).
    assert "HARNESS_CLAUDE_SDK_API_KEY_HELPER" in env


def test_cwd_param_threads_harness_claude_sdk_cwd(tmp_path: Path) -> None:
    """
    The ``cwd`` parameter threads the session workspace to the Claude
    SDK subprocess as ``HARNESS_CLAUDE_SDK_CWD``.

    The runner resolves the session's stored ``workspace`` to a
    per-session cwd and passes it through. Without threading it, the
    Claude SDK CLI would fall back to ``os_env.cwd`` and then to the
    runner's inherited cwd — which for a Control Room V2 session is the
    runner's checkout, NOT the disposable git repo the user pointed the
    session at. The orchestrator's git commands and worker dispatch
    would target the wrong tree.

    Failure here means the Claude SDK orchestrator silently runs in the
    wrong directory, looks for the wrong files, and never sees the
    session workspace the user actually asked for.
    """
    spec = _make_spec(model="custom/best-coding")
    session_workspace = tmp_path / "session-workspace"
    env = _build_claude_sdk_spawn_env(spec, cwd=session_workspace, workdir=None)
    assert env["HARNESS_CLAUDE_SDK_CWD"] == str(session_workspace)
    # CWD is independent of the bundle dir (workdir): setting one does
    # not also set the other. A session workspace is where the SDK
    # operates; the bundle dir is where the harness finds bundled
    # skills. Same value here would just be coincidence.
    assert "HARNESS_CLAUDE_SDK_BUNDLE_DIR" not in env or env.get(
        "HARNESS_CLAUDE_SDK_BUNDLE_DIR"
    ) != str(session_workspace)


def test_cwd_omitted_does_not_set_harness_claude_sdk_cwd() -> None:
    """
    Omitting ``cwd`` must not set ``HARNESS_CLAUDE_SDK_CWD``.

    The Claude SDK harness wrap falls back to ``os_env.cwd`` and then
    the inherited subprocess cwd when the env var is absent. Setting it
    unconditionally (even when the caller passes ``None``) would
    override a spec's ``os_env.cwd: .`` resolution with the literal
    string ``"None"`` — a regression of #1423-style silently-broken
    launches.
    """
    spec = _make_spec(model="custom/best-coding")
    env = _build_claude_sdk_spawn_env(spec, workdir=None)
    assert "HARNESS_CLAUDE_SDK_CWD" not in env
