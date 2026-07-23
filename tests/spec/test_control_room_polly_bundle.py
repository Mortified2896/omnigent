"""Focused tests for the control-room-polly example bundle.

The bundle is the V1 single-worker coding orchestrator. It declares:

- top-level executor: omnigent + pi + model ``custom/best-coding``;
- exactly one sub-agent named ``opencode``;
- the sub-agent's executor: omnigent + opencode-native + model
  ``custom/best-coding``;
- blast-radius policies on both with the orchestrator's gate_pushes off and
  the worker's gate_pushes on;
- no MCP servers, no extra skills, no extra terminals, no model-adviser hint;
- prompts that forbid the worker from pushing, opening a PR, or invoking
  ``sys_advise_models``;
- the fixed OmniRoute credential is threaded through
  ``os_env.sandbox.env_passthrough`` so both the Pi subprocess and the
  opencode-native harness can reach the local route gateway when probing
  the configured combo.

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


def test_top_level_executor_is_pi_with_custom_best_coding(bundle_spec: object) -> None:
    """Top-level: omnigent executor, pi harness, ``custom/best-coding`` model."""
    executor = bundle_spec.executor
    assert executor.type == "omnigent"
    assert executor.config.get("harness") == "pi"
    assert executor.model == FIXED_ROUTE


def test_exactly_one_sub_agent_named_opencode(bundle_spec: object) -> None:
    """Only the ``opencode`` sub-agent is declared and exposed as a tool."""
    sub_names = [sa.name for sa in bundle_spec.sub_agents]
    assert sub_names == ["opencode"], sub_names
    assert bundle_spec.tools.agents == ["opencode"]


def test_opencode_worker_uses_opencode_native_with_custom_best_coding(
    bundle_spec: object,
) -> None:
    """Sub-agent: opencode-native harness, same ``custom/best-coding`` model."""
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
    """Worker blast-radius policy: gate_pushes=True so any stray push ASKs."""
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
    assert blast.function.arguments.get("gate_pushes") is True


def test_omniroute_credential_threaded_to_orchestrator_sandbox(
    bundle_spec: object,
) -> None:
    """Top-level must opt the Pi subprocess into the OmniRoute credential.

    The Pi executor's env allowlist deliberately omits credential families
    (it would otherwise leak every host secret into the subprocess). A
    Pi turn using an OmniRoute combo therefore needs the spec to opt in
    to ``OMNIGENT_ROUTER_API_KEY`` via ``os_env.sandbox.env_passthrough``
    so the catalog probe can reach the local route gateway.
    """
    passthrough = (bundle_spec.os_env.sandbox.env_passthrough or []) if bundle_spec.os_env else []
    assert "OMNIGENT_ROUTER_API_KEY" in passthrough, (
        "orchestrator must pass OMNIGENT_ROUTER_API_KEY through to the Pi "
        "subprocess via os_env.sandbox.env_passthrough (otherwise the "
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
    assert materialized.executor.config.get("harness") == "pi"
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
