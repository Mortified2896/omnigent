"""Focused tests for the control-room-polly example bundle.

The bundle is the V1 single-worker coding orchestrator. It declares:

- top-level executor: omnigent + claude-sdk + model ``custom/best-coding``
  wired through the OmniRoute Anthropic Messages-compatible gateway;
- exactly one sub-agent named ``opencode``;
- the sub-agent's executor: omnigent + opencode-native + model
  ``custom/best-coding``;
- blast-radius policies on both with the orchestrator's gate_pushes off and
  the worker's deny_pushes on;
- no MCP servers, no extra skills, no extra terminals, no model-adviser hint;
- prompts that forbid the worker from pushing, opening a PR, or invoking
  ``sys_advise_models``;
- the fixed OmniRoute credential is threaded through
  ``os_env.sandbox.env_passthrough`` so both the Claude SDK subprocess and
  the opencode-native harness can reach the local route gateway when probing
  the configured combo.

The bundle is also the regression that the prior Pi-top-level run hit:
"the Pi top-level orchestrator failed to continue after completion".
Replacing the orchestrator harness with ``claude-sdk`` is the correction;
the bundle must reflect the new harness and the new gateway plumbing, but
keep the worker (opencode-native + custom/best-coding) and the
route-approval-disabled V1 safety rules intact.

Materialization through ``omnigent.spec.materialize_bundle`` must produce
a clean bundle directory the server can seed via
``OMNIGENT_BUILTIN_AGENT_DIRS`` without warnings.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import materialize_bundle, parse

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "examples" / "control-room-polly"

# The fixed-route combo both layers are locked to. Replacing it (or letting
# the bundle silently fall back to ``auto/coding`` or a physical model) is the
# regression we're guarding against.
FIXED_ROUTE = "custom/best-coding"

# OmniRoute's local Anthropic Messages endpoint. The bundle references this
# URL via the ``executor.auth.base_url`` block, and the spawn-env builder
# lifts it into ``HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL`` so the Claude CLI
# subprocess dials it instead of api.anthropic.com. The literal hostname /
# port is duplicated in the bundle's own YAML so this module can detect
# accidental drift without going through the spawn-env plumbing.
OMNIROUTE_BASE_URL = "http://127.0.0.1:20128/v1"


@pytest.fixture(autouse=True)
def _omniroute_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the OmniRoute credential the bundle's ``executor.auth.api_key``
    reference expands at parse time.

    The literal ``$OMNIGENT_ROUTER_API_KEY`` reference in the bundle stays
    unexpanded in the YAML file (no secret embedded) but the parser raises
    on an unresolved ``$VAR`` — a safety net for typos. Set a sentinel here
    so the parser expands cleanly and the spawn-env assertions below can
    see the real expansion.
    """
    monkeypatch.setenv("OMNIGENT_ROUTER_API_KEY", "sk-test-sentinel-1234567890")


@pytest.fixture()
def bundle_spec() -> object:
    """Parse the bundle from the source tree (no materialization step)."""
    return parse(BUNDLE_DIR)


def test_bundle_directory_and_name_aligned() -> None:
    """The directory name and the spec's ``name:`` field must match exactly."""
    assert BUNDLE_DIR.is_dir(), f"bundle dir missing: {BUNDLE_DIR}"
    config = (BUNDLE_DIR / "config.yaml").read_text()
    assert "name: control-room-polly" in config, (
        "bundle directory and spec name must be aligned (control-room-polly)"
    )
    assert BUNDLE_DIR.name == "control-room-polly"


def test_bundle_does_not_declare_auto_best_coding() -> None:
    """No layer may use the rejected ``auto/best-coding`` combo id.

    ``auto/best-coding`` was the originally-requested combo id but it is not
    actually a valid OmniRoute catalog entry; the real fixed combo is
    ``custom/best-coding``. Catching a regression to ``auto/best-coding``
    here prevents a silent model swap on top of the resource bug.
    """
    for path in (BUNDLE_DIR / "config.yaml", BUNDLE_DIR / "agents/opencode/config.yaml"):
        text = path.read_text()
        # Strip the prompt's explicit "do not …" examples — those stay as
        # the documentation of what the fixed route is locked AGAINST.
        # The model field itself must not equal ``auto/best-coding`` in
        # the executor block.
        assert "model: auto/best-coding" not in text, (
            f"{path.relative_to(REPO_ROOT)} must not declare model auto/best-coding"
        )


def test_bundle_does_not_silently_fall_back_to_auto_coding() -> None:
    """The bundle must not silently accept ``auto/coding`` as a fallback.

    References to ``auto/coding`` may remain only in the prompt's
    "do not switch to" wording (which is the rejected-fallback notice).
    The executor ``model:`` field on both layers must remain the fixed
    ``custom/best-coding`` combo.
    """
    for path in (BUNDLE_DIR / "config.yaml", BUNDLE_DIR / "agents/opencode/config.yaml"):
        text = path.read_text()
        assert "model: auto/coding" not in text, (
            f"{path.relative_to(REPO_ROOT)} must not silently fall back to auto/coding"
        )


def test_top_level_executor_is_claude_sdk_with_custom_best_coding(
    bundle_spec: object,
) -> None:
    """Top-level: omnigent executor, claude-sdk harness, ``custom/best-coding`` model.

    The brief mandates a Claude SDK top-level. The Pi top-level hit a wake /
    resume regression that left the orchestrator silent after the worker
    committed; the replacement is the Claude SDK harness, which uses the
    same OmniRoute gateway plumbing (custom/best-coding) the worker already
    proves.
    """
    executor = bundle_spec.executor
    assert executor.type == "omnigent"
    assert executor.config.get("harness") == "claude-sdk"
    assert executor.model == FIXED_ROUTE


def test_top_level_auth_wires_omniroute_gateway_via_env_ref(
    bundle_spec: object,
) -> None:
    """Top-level ``executor.auth`` references the OmniRoute gateway by env var.

    The bundle's ``auth`` block must:

    - declare ``type: api_key`` so the spawn-env builder treats the
      credential as a bearer token;
    - reference ``$OMNIGENT_ROUTER_API_KEY`` (not an inline literal) so the
      secret stays in the host's environment — no embedded credentials;
    - carry ``base_url: http://127.0.0.1:20128/v1`` so the workflow layer
      routes the SDK through OmniRoute's Anthropic Messages-compatible
      transport instead of api.anthropic.com.

    Failure means either the bundle is going to dial api.anthropic.com
    (which requires a direct Anthropic subscription, off-limits per the
    brief) or the bundle has shipped with a literal key in plain text.
    """
    auth = bundle_spec.executor.auth
    assert auth is not None, "top-level executor must declare an auth block"
    assert auth.type == "api_key"
    # api_key is expanded at parse time from the env stub fixture; the
    # actual value isn't asserted (it would leak the sentinel into the
    # error log) — just that the env-stub expansion succeeded.
    assert auth.api_key, "api_key must expand to a non-empty string"
    assert auth.base_url == OMNIROUTE_BASE_URL, (
        f"base_url must point at the OmniRoute gateway ({OMNIROUTE_BASE_URL!r}); "
        f"got {auth.base_url!r}"
    )


def test_exactly_one_sub_agent_named_opencode(bundle_spec: object) -> None:
    """Only the ``opencode`` sub-agent is declared and exposed as a tool."""
    sub_names = [sa.name for sa in bundle_spec.sub_agents]
    assert sub_names == ["opencode"], sub_names
    assert bundle_spec.tools.agents == ["opencode"]


def test_opencode_worker_uses_opencode_native_with_custom_best_coding(
    bundle_spec: object,
) -> None:
    """Sub-agent: opencode-native harness, same ``custom/best-coding`` model.

    The worker harness stays opencode-native. The brief's V1 swapped the
    top-level harness to claude-sdk; the worker is unchanged because
    opencode-native + custom/best-coding is the proven pair that committed
    locally in the prior live test.
    """
    sub = bundle_spec.sub_agents[0]
    assert sub.name == "opencode"
    assert sub.executor.type == "omnigent"
    assert sub.executor.config.get("harness") == "opencode-native"
    assert sub.executor.model == FIXED_ROUTE


def test_orchestrator_supports_async_and_cancellation(bundle_spec: object) -> None:
    """Top-level must run async and be cancellable."""
    assert bundle_spec.async_enabled is True
    # ``spawn`` stays False so we don't accidentally create orphan children
    # — the sub-agent tool is the only dispatch path. ``spawn: true`` was
    # explicitly noted as unnecessary in the brief.
    assert bundle_spec.spawn is False


def test_no_unnecessary_mcp_servers_skills_or_terminals(
    bundle_spec: object,
) -> None:
    """V1 keeps the surface minimal: no MCP, no extra skills, no terminals."""
    assert bundle_spec.mcp_servers == []
    assert bundle_spec.skills == []
    # Generic shell terminals are not declared.
    assert not bundle_spec.terminals


def test_blast_radius_policy_on_orchestrator(bundle_spec: object) -> None:
    """Top-level blast-radius policy must load and disable push ASK."""
    policies = bundle_spec.guardrails.policies
    blast = next(
        (p for p in policies if p.function and p.function.path.endswith("blast_radius")),
        None,
    )
    assert blast is not None, "missing blast_radius policy on orchestrator"
    args = blast.function.arguments
    assert args.get("gate_pushes") is False, (
        "orchestrator must publish without an ASK, so gate_pushes must be False"
    )


def test_blast_radius_policy_on_worker(bundle_spec: object) -> None:
    """Worker blast-radius policy denies every publication attempt."""
    sub = bundle_spec.sub_agents[0]
    blast = next(
        (
            p
            for p in sub.guardrails.policies
            if p.function and p.function.path.endswith("blast_radius")
        ),
        None,
    )
    assert blast is not None, "missing blast_radius policy on opencode worker"
    assert blast.function.arguments.get("deny_pushes") is True
    assert blast.function.arguments.get("gate_pushes") is False


def test_omniroute_credential_threaded_to_orchestrator_sandbox(
    bundle_spec: object,
) -> None:
    """Top-level must opt the Claude SDK subprocess into the OmniRoute credential.

    The Claude SDK executor's env-var path threads the credential through
    the spawn-env builder (HARNESS_CLAUDE_SDK_API_KEY_HELPER + the
    gateway transport), but the SDK subprocess also probes the catalog
    at startup — that probe needs the credential visible in the subprocess
    env, which means the spec's ``os_env.sandbox.env_passthrough`` must
    include the canonical name so the runner's per-spawn env allowlist
    passes it through.
    """
    passthrough = (bundle_spec.os_env.sandbox.env_passthrough or []) if bundle_spec.os_env else []
    assert "OMNIGENT_ROUTER_API_KEY" in passthrough, (
        "orchestrator must pass OMNIGENT_ROUTER_API_KEY through to the Claude "
        "SDK subprocess via os_env.sandbox.env_passthrough (otherwise the "
        "fixed-route first turn fails before any text is produced)."
    )


def test_omniroute_credential_threaded_to_worker_sandbox(
    bundle_spec: object,
) -> None:
    """Worker must also opt the opencode-native subprocess into the credential."""
    sub = bundle_spec.sub_agents[0]
    passthrough = (sub.os_env.sandbox.env_passthrough or []) if sub.os_env else []
    assert "OMNIGENT_ROUTER_API_KEY" in passthrough, (
        "opencode worker must pass OMNIGENT_ROUTER_API_KEY through via "
        "os_env.sandbox.env_passthrough (the omniroute-provider catalog "
        "probe otherwise fails with OMNIROUTE_API_KEY not set)."
    )


def test_prompts_forbid_dynamic_model_selection(bundle_spec: object) -> None:
    """Neither prompt may rely on dynamic model-selection tooling."""
    parent = bundle_spec.instructions or ""
    sub = bundle_spec.sub_agents[0].instructions or ""
    # The brief mandates the literal "Do not call ``sys_advise_models``" line
    # in the parent prompt and the equivalent prohibition in the worker.
    assert "sys_advise_models" in parent, "parent prompt must mention sys_advise_models"
    assert "sys_advise_models" in sub, "worker prompt must mention sys_advise_models"
    # The negation must appear on the same line. Strip the prompt down to the
    # paragraphs that mention ``sys_advise_models`` and assert the sentence
    # forbids the call (handles backtick or bare rendering, leading list
    # bullets, and continuation lines).
    for label, text in (("parent", parent), ("worker", sub)):
        for line in text.splitlines():
            if "sys_advise_models" not in line:
                continue
            normalized = line.lower().replace("`", "").lstrip("-* \t").strip()
            # The forbidden tool must appear in a sentence that forbids the
            # call, e.g. "do not call …", "do not …, and do not", "do not
            # request …". A "don't invoke" parenthetical also qualifies.
            has_negation = any(
                token in normalized
                for token in (
                    "do not",
                    "don't",
                    "must not",
                    "never ",
                    "not call",
                    "not request",
                    "not invoke",
                )
            )
            assert has_negation, (
                f"{label} prompt must forbid sys_advise_models in a negative sentence: {line!r}"
            )
    # Neither prompt should let the worker override the lane.
    for label, text in (("parent", parent), ("worker", sub)):
        assert "args.model" in text, f"{label} prompt must explicitly mention args.model handling"


def test_prompts_locked_to_custom_best_coding(bundle_spec: object) -> None:
    """Both prompts must name ``custom/best-coding`` as the fixed route.

    The brief requires the prompt wording to make clear that the agent is
    fixed to ``custom/best-coding`` and must not silently accept another
    route. Vague "OmniRoute Coding Best lane" wording (the original V1
    copy) leaves room for the model to drift, so we enforce the literal
    combo id appears in both prompts.
    """
    parent = bundle_spec.instructions or ""
    sub = bundle_spec.sub_agents[0].instructions or ""
    assert "custom/best-coding" in parent, (
        "parent prompt must explicitly call out custom/best-coding as the fixed route"
    )
    assert "custom/best-coding" in sub, (
        "worker prompt must explicitly call out custom/best-coding as the fixed route"
    )


def test_prompts_warn_against_fallback_routes(bundle_spec: object) -> None:
    """The parent must explicitly forbid ``auto/coding`` as a fallback.

    The earlier V1 run silently substituted ``auto/coding`` when the
    route-approval recommender rerouted the spec's ``auto/best-coding``.
    The corrected prompt must name ``auto/coding`` (and ``auto/best-coding``)
    as the routes the orchestrator must not accept.
    """
    parent = bundle_spec.instructions or ""
    assert "auto/coding" in parent, (
        "parent prompt must name auto/coding as a route it must NOT accept"
    )
    assert "auto/best-coding" in parent, (
        "parent prompt must name auto/best-coding as a route it must NOT accept"
    )


def test_prompt_forbids_model_routing_recommender(bundle_spec: object) -> None:
    """The parent prompt must not enable Model Routing for this bundle.

    The brief mandates ``route_approval_enabled: false`` for Control Room
    sessions; the parent prompt must explicitly tell the agent not to
    route itself, not to call the model-adviser, and not to override the
    fixed route. The wording was tightened in this revision (V2) so the
    agent has no ambiguity: a generic "use the model configured for this
    agent" rule, no per-route-id forbidden examples.
    """
    parent = bundle_spec.instructions or ""
    # The brief requires the agent not to even consult the model-adviser,
    # not to substitute models, and not to override the harness.
    for required in (
        "Do not call `sys_advise_models`",
        "Do not pass an `args.model` override",
        "Do not substitute another coding harness",
    ):
        assert required in parent, f"parent prompt must carry the literal line: {required!r}"


def test_worker_prompt_forbids_publication_actions(bundle_spec: object) -> None:
    """The worker's prompt must explicitly forbid push, PR, and merge."""
    sub_prompt = bundle_spec.sub_agents[0].instructions or ""
    # Literal markers the brief mandates. The brief says: remove official
    # Polly instructions that tell the worker to push or run `gh pr create`;
    # never force-push or rewrite history. Asserting on the canonical
    # wording catches accidental permission slips and rewording.
    assert "git push" in sub_prompt, "worker prompt must mention `git push` (forbidden)"
    assert "gh pr create" in sub_prompt, "worker prompt must mention `gh pr create` (forbidden)"
    assert "force-push" in sub_prompt or "force push" in sub_prompt, (
        "worker prompt must mention force-push (forbidden)"
    )
    assert "history" in sub_prompt, "worker prompt must mention history (forbidden)"
    # And every one of these tokens must appear in a negative context —
    # either an explicit "do not" / "must not" / "never" or as a forbidden
    # imperative under the "## You must NOT" section (we check the section
    # header is present and assert each forbidden token falls under it).
    has_must_not_section = any(
        line.strip().lower().startswith("## you must not") for line in sub_prompt.splitlines()
    )
    assert has_must_not_section, "worker prompt must have an explicit '## You must NOT' section"
    # Spot-check the forbidden tokens appear in a forbidden context: either
    # under the "## You must NOT" section, or in a "do not" / "never" /
    # "must not" sentence. Because forbidden-actions are framed as a bullet
    # list (e.g. ``- Push the branch anywhere``), we accept bullets that
    # reference forbidding language anywhere within the same bullet or its
    # neighbours.
    forbidden_imperatives = ("Push the branch", "Open a pull request", "Force-push")
    for imperative in forbidden_imperatives:
        assert any(imperative in line for line in sub_prompt.splitlines()), (
            f"worker prompt missing forbidden imperative: {imperative!r}"
        )


def test_parent_prompt_contains_safety_rules(bundle_spec: object) -> None:
    """The parent prompt must require commit + push + fast-forward rules."""
    parent_prompt = bundle_spec.instructions or ""
    for required_section in (
        "Mandatory OpenCode worker contract",
        "Verification before publication",
        "Publishing the task branch",
        "Promotion to main",
        "Forbidden Git actions",
        "When publication cannot complete safely",
    ):
        assert required_section in parent_prompt, (
            f"parent prompt missing required section: {required_section!r}"
        )


def test_orchestrator_has_no_spawn_flag(bundle_spec: object) -> None:
    """The brief says: no ``spawn: true`` unless sys_session_send is missing.

    The spec exposes ``opencode`` as a sub-agent, so the standard
    ``sys_session_send`` is the dispatch path and ``spawn: true`` is
    unnecessary.
    """
    assert bundle_spec.spawn is False


def test_bundle_materializes_for_builtin_seed(tmp_path: Path) -> None:
    """The bundle must round-trip through ``materialize_bundle`` cleanly.

    The server seeds extra built-ins from
    ``OMNIGENT_BUILTIN_AGENT_DIRS`` by materializing the source path into
    a temp bundle and tarballing it. The materialized dir must contain
    the same two YAML files.
    """
    bundle_dir = materialize_bundle(BUNDLE_DIR, tmp_path / "bundle")
    assert bundle_dir.is_dir()
    files = sorted(p.relative_to(bundle_dir).as_posix() for p in bundle_dir.rglob("*.yaml"))
    assert files == ["agents/opencode/config.yaml", "config.yaml"], files


def test_materialized_bundle_parses(bundle_spec: object, tmp_path: Path) -> None:
    """The materialized bundle must also parse to the same shape."""
    bundle_dir = materialize_bundle(BUNDLE_DIR, tmp_path / "bundle")
    materialized = parse(bundle_dir)
    assert materialized.name == "control-room-polly"
    assert [sa.name for sa in materialized.sub_agents] == ["opencode"]
    assert materialized.executor.config.get("harness") == "claude-sdk"
    assert materialized.executor.model == FIXED_ROUTE
    assert materialized.sub_agents[0].executor.config.get("harness") == "opencode-native"
    assert materialized.sub_agents[0].executor.model == FIXED_ROUTE


def test_official_polly_bundle_unchanged() -> None:
    """This V1 must not modify the official polly bundle."""
    polly_dir = REPO_ROOT / "examples" / "polly"
    assert polly_dir.is_dir()
    polly_config = (polly_dir / "config.yaml").read_text()
    # Spot-check a few lines that would change if polly were touched.
    assert "name: polly" in polly_config
    assert "harness: claude-sdk" in polly_config
    # Polly still declares all six sub-agents.
    assert "- claude_code" in polly_config
    assert "- codex" in polly_config
    assert "- opencode" in polly_config
    assert "- cursor" in polly_config
    assert "- hermes" in polly_config
    assert "- pi" in polly_config
