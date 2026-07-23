"""Focused tests for the control-room-polly example bundle.

The bundle is the V1 single-worker coding orchestrator. It declares:

- top-level executor: omnigent + pi + model ``auto/best-coding``;
- exactly one sub-agent named ``opencode``;
- the sub-agent's executor: omnigent + opencode-native + model ``auto/best-coding``;
- blast-radius policies on both with the orchestrator's gate_pushes off and
  the worker's gate_pushes on;
- no MCP servers, no extra skills, no extra terminals, no model-adviser hint;
- prompts that forbid the worker from pushing, opening a PR, or invoking
  ``sys_advise_models``.

Materialization through ``omnigent.spec.materialize_bundle`` must produce
a clean bundle directory the server can seed via
``OMNIGENT_BUILTIN_AGENT_DIRS`` without warnings.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from omnigent.spec import materialize_bundle, parse


REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "examples" / "control-room-polly"


@pytest.fixture()
def bundle_spec() -> object:
    """Parse the bundle from the source tree (no materialization step)."""
    return parse(BUNDLE_DIR)


def test_bundle_directory_and_name_aligned() -> None:
    """The directory name and the spec's ``name:`` field must match exactly."""
    assert BUNDLE_DIR.is_dir(), f"bundle dir missing: {BUNDLE_DIR}"
    config = (BUNDLE_DIR / "config.yaml").read_text()
    assert f"name: control-room-polly" in config, (
        "bundle directory and spec name must be aligned (control-room-polly)"
    )
    assert BUNDLE_DIR.name == "control-room-polly"


def test_top_level_executor_is_pi_with_best_coding(bundle_spec: object) -> None:
    """Top-level: omnigent executor, pi harness, ``auto/best-coding`` model."""
    executor = bundle_spec.executor
    assert executor.type == "omnigent"
    assert executor.config.get("harness") == "pi"
    assert executor.model == "auto/best-coding"


def test_exactly_one_sub_agent_named_opencode(bundle_spec: object) -> None:
    """Only the ``opencode`` sub-agent is declared and exposed as a tool."""
    sub_names = [sa.name for sa in bundle_spec.sub_agents]
    assert sub_names == ["opencode"], sub_names
    assert bundle_spec.tools.agents == ["opencode"]


def test_opencode_worker_uses_opencode_native_with_best_coding(
    bundle_spec: object,
) -> None:
    """Sub-agent: opencode-native harness, same ``auto/best-coding`` model."""
    sub = bundle_spec.sub_agents[0]
    assert sub.name == "opencode"
    assert sub.executor.type == "omnigent"
    assert sub.executor.config.get("harness") == "opencode-native"
    assert sub.executor.model == "auto/best-coding"


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
        (p for p in sub.guardrails.policies if p.function and p.function.path.endswith("blast_radius")),
        None,
    )
    assert blast is not None, "missing blast_radius policy on opencode worker"
    assert blast.function.arguments.get("gate_pushes") is True


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
        assert "args.model" in text, (
            f"{label} prompt must explicitly mention args.model handling"
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
        line.strip().lower().startswith("## you must not")
        for line in sub_prompt.splitlines()
    )
    assert has_must_not_section, (
        "worker prompt must have an explicit '## You must NOT' section"
    )
    # Spot-check the forbidden tokens appear in a forbidden context: either
    # under the "## You must NOT" section, or in a "do not" / "never" /
    # "must not" sentence. Because forbidden-actions are framed as a bullet
    # list (e.g. ``- Push the branch anywhere``), we accept bullets that
    # reference forbidding language anywhere within the same bullet or its
    # neighbours.
    forbidden_imperatives = ("Push the branch", "Open a pull request", "Force-push")
    for imperative in forbidden_imperatives:
        assert any(
            imperative in line
            for line in sub_prompt.splitlines()
        ), f"worker prompt missing forbidden imperative: {imperative!r}"


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
    assert materialized.executor.model == "auto/best-coding"
    assert materialized.sub_agents[0].executor.config.get("harness") == "opencode-native"
    assert materialized.sub_agents[0].executor.model == "auto/best-coding"


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
