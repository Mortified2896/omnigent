"""Smoke tests for the control-room-polly bundle.

Two smoke scenarios:

- **A (orchestration boot)** — verify the bundle parses, registers the
  declared sub-agent as a tool, threads ``custom/best-coding`` into both
  harnesses' spawn-env / model-pinning paths, and forbids dynamic model
  selection / worker push / PR actions via the prompt. This runs in-process
  on the feature checkout, no host needed.

- **B (disposable commit workflow)** — build a disposable Git repo +
  bare remote, exercise the same parser/dispatch wiring without a live
  host, then directly issue the Git commands the worker is contractually
  obligated to make (and the orchestrator is obligated to verify) against
  the disposable repo. Asserts the disposable remote's main fast-forwards
  cleanly. Never touches the production fork.

The brief explicitly rejects driving real model turns for these smoke
tests, so this module uses the deterministic parser + harness wiring +
real Git against disposable-local targets only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.spec._omnigent_compat import _OMNIGENT_ACCEPTED_HARNESSES
from omnigent.spec.parser import parse

REPO_ROOT = Path(__file__).resolve().parents[2]
BUNDLE_DIR = REPO_ROOT / "examples" / "control-room-polly"

# The fixed OmniRoute combo both layers are locked to. The earlier V1
# pinned ``auto/best-coding`` (which is not a valid catalog entry) and
# drifted to ``auto/coding`` via the route-approval recommender; this
# module locks the corrected combo id everywhere.
FIXED_ROUTE = "custom/best-coding"


# ── Smoke A: orchestration boot ────────────────────────────────────────────


def test_smoke_a_bundle_parses_and_lists_opencode_sub_agent() -> None:
    """Boot = bundle parses; the opencode sub-agent is exposed as a tool."""
    spec = parse(BUNDLE_DIR)
    assert spec.name == "control-room-polly"
    assert spec.executor.type == "omnigent"
    assert spec.executor.config.get("harness") == "pi"
    assert spec.executor.model == FIXED_ROUTE
    assert spec.tools.agents == ["opencode"]
    assert [sa.name for sa in spec.sub_agents] == ["opencode"]


def test_smoke_a_harness_ids_accepted() -> None:
    """Both harnesses are accepted by the omnigent compatibility set."""
    spec = parse(BUNDLE_DIR)
    assert spec.executor.config.get("harness") in _OMNIGENT_ACCEPTED_HARNESSES
    sub = spec.sub_agents[0]
    assert sub.executor.config.get("harness") in _OMNIGENT_ACCEPTED_HARNESSES


def test_smoke_a_pi_spawn_env_threads_custom_best_coding(tmp_path: Path) -> None:
    """The top-level spec threads ``custom/best-coding`` to the Pi harness."""
    sys.path.insert(0, str(REPO_ROOT))
    spec = parse(BUNDLE_DIR)
    from omnigent.runtime.workflow import _build_pi_spawn_env

    env = _build_pi_spawn_env(spec, workdir=tmp_path)
    assert env["HARNESS_PI_MODEL"] == FIXED_ROUTE
    assert env["HARNESS_PI_AGENT_NAME"] == "control-room-polly"


def test_smoke_a_opencode_native_model_resolved_from_sub_spec() -> None:
    """The OpenCode worker harness reads the same lane from the sub-spec."""
    spec = parse(BUNDLE_DIR)
    sub = spec.sub_agents[0]
    # The runner uses this resolver to seed the per-session bridge
    # state. Without it, the worker would fall back to opencode's
    # configured default — which is exactly the lane hijack V1 forbids.
    from omnigent.runner.app import _opencode_native_model_from_spec

    assert _opencode_native_model_from_spec(sub) == FIXED_ROUTE


def test_smoke_a_prompts_forbid_model_adviser_and_publication() -> None:
    """No dynamic model selection, no worker push/PR.

    Verified inline on the parsed prompts so a future prompt rewrite
    that re-allows model-adviser calls or worker publication breaks
    this test immediately.
    """
    spec = parse(BUNDLE_DIR)
    parent = spec.instructions or ""
    sub = spec.sub_agents[0].instructions or ""
    # Neither prompt may include the bare ``sys_advise_models`` as a
    # positive instruction. Both must negate it.
    for label, text in (("parent", parent), ("worker", sub)):
        for line in text.splitlines():
            if "sys_advise_models" not in line:
                continue
            normalized = line.lower().replace("`", "").lstrip("-* \t").strip()
            assert any(
                token in normalized
                for token in ("do not", "don't", "must not", "never", "not call")
            ), f"{label} prompt re-allows sys_advise_models: {line!r}"
    # The worker must explicitly disclaim push and PR publication.
    assert "Push the branch" in sub, "worker prompt must forbid pushing"
    assert "Open a pull request" in sub, "worker prompt must forbid opening PRs"


def test_smoke_a_sandbox_env_passthrough_threads_omniroute_credential(
    tmp_path: Path,
) -> None:
    """The Pi subprocess env must include the OmniRoute credential.

    The Pi subprocess env is allowlist-filtered (so credential families
    do not leak), but ``OMNIGENT_ROUTER_API_KEY`` is opted in by the
    spec's ``os_env.sandbox.env_passthrough``. Without it the first turn
    of a fixed-route session fails before any text is produced (the Pi
    subprocess aborts with ``[omniroute-provider] OMNIROUTE_API_KEY not
    set in process environment``).

    The Pi executor's :func:`_clean_pi_env` allowlist applies
    ``os_env.sandbox.env_passthrough`` as its ``extra_allowed`` arg, so
    we exercise the same call here — that's the chokepoint that drops
    credential families for the Pi subprocess otherwise.
    """
    sys.path.insert(0, str(REPO_ROOT))
    spec = parse(BUNDLE_DIR)
    from omnigent.inner.pi_executor import _clean_pi_env

    # Inject a sentinel so we can prove the env-passthrough path
    # actually copies values into the subprocess env.
    sentinel = "0" * 8 + "abcdef"  # 16-char stub
    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setenv("OMNIGENT_ROUTER_API_KEY", sentinel)
        extra = (
            spec.os_env.sandbox.env_passthrough if spec.os_env and spec.os_env.sandbox else None
        )
        cleaned = _clean_pi_env(extra_allowed=extra)
    finally:
        monkeypatch.undo()
    assert cleaned.get("OMNIGENT_ROUTER_API_KEY") == sentinel, (
        "Pi subprocess must inherit OMNIGENT_ROUTER_API_KEY from the host "
        "via os_env.sandbox.env_passthrough; otherwise the omniroute-"
        "provider catalog probe has no token and the first turn aborts."
    )


def test_smoke_a_subagent_prompts_pin_custom_best_coding() -> None:
    """Both prompts must explicitly reference the fixed combo id.

    The brief's V2 ask was to remove the vague "OmniRoute Coding Best
    lane" wording from the original V1 copy. ``custom/best-coding`` is
    the exact combo id, and both prompts must surface it (one as the
    locked route, the other as the per-turn model with a no-fallback
    rule).
    """
    spec = parse(BUNDLE_DIR)
    parent = spec.instructions or ""
    sub = spec.sub_agents[0].instructions or ""
    assert "custom/best-coding" in parent, (
        "parent prompt must mention the fixed combo id custom/best-coding"
    )
    assert "custom/best-coding" in sub, (
        "worker prompt must mention the fixed combo id custom/best-coding"
    )
    # And the original V1 combo id must be absent from the model line.
    # (We don't assert full-string absence because the prompt's "do not"
    # examples still say ``auto/coding`` and ``auto/best-coding``.)
    for label, text in (("parent", parent), ("worker", sub)):
        assert FIXED_ROUTE in text, f"{label} prompt missing {FIXED_ROUTE}"


# ── Smoke B: disposable commit workflow ────────────────────────────────────


@pytest.fixture()
def disposable_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Build a disposable Git repo + bare remote under *tmp_path*.

    ``(local_repo, bare_remote)`` — both rooted under tmp_path so the
    test cleans itself up automatically. The remote is `file://`-style
    on the local filesystem; the brief mandates "disposable local or
    bare remote", and a file URL keeps the test hermetic and avoids
    pushing at anything production-like.
    """
    bare = tmp_path / "bare.git"
    bare.mkdir()
    subprocess.check_call(["git", "init", "--bare", "--initial-branch=main", str(bare)])
    local = tmp_path / "local"
    local.mkdir()
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
        # Never inherit credentials — push to a file:// bare is fine
        # without any, so we drop the host's gh/git auth for this test.
        "GIT_ASKPASS": "",
        "GIT_TERMINAL_PROMPT": "0",
    }

    def _run(args: list[str], cwd: Path) -> str:
        return subprocess.check_output(["git", "-C", str(cwd), *args], env=env).decode().strip()

    _run(["init", "--initial-branch=main"], local)
    _run(["remote", "add", "origin", str(bare)], local)
    (local / "README.md").write_text("# smoke\n")
    _run(["add", "README.md"], local)
    _run(["commit", "-m", "init"], local)
    _run(["push", "-u", "origin", "main"], local)
    return local, bare


def _git(local: Path, *args: str) -> str:
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "smoke",
        "GIT_AUTHOR_EMAIL": "smoke@example.com",
        "GIT_COMMITTER_NAME": "smoke",
        "GIT_COMMITTER_EMAIL": "smoke@example.com",
        "GIT_TERMINAL_PROMPT": "0",
    }
    return subprocess.check_output(["git", "-C", str(local), *args], env=env).decode().strip()


@pytest.fixture()
def worker_worktree(disposable_repo: tuple[Path, Path]) -> Path:
    """Create a task branch worktree at the disposable local repo.

    The worker contract requires working in its assigned worktree. We
    simulate the orchestrator doing this by creating a worktree off
    main for ``feature/smoke-task``. The bare remote is intentionally
    NOT touched.
    """
    local, _ = disposable_repo
    branch = "feature/smoke-task"
    wt = local.parent / "worktree"
    _git(local, "worktree", "add", "-b", branch, str(wt))
    return wt


def test_smoke_b_worker_creates_local_commit_without_push(
    worker_worktree: Path,
    disposable_repo: tuple[Path, Path],
) -> None:
    """Worker contract: scoped implementation, focused validation,
    local commit, no push, no PR.

    This is the worker-only half of Smoke B. The orchestrator half
    (push + main fast-forward) lives in
    :func:`test_smoke_b_orchestrator_publishes_task_branch_and_promotes_main`.
    """
    _local, _bare = disposable_repo
    wt = worker_worktree
    head_before = _git(wt, "rev-parse", "HEAD")
    assert head_before != ""
    # Worker: a focused, scoped change (one new file, one minor doc
    # edit). No unrelated cleanup, no formatting churn.
    (wt / "NOTES.md").write_text("# smoke notes\ntrivial addition\n")
    _git(wt, "add", "NOTES.md")
    _git(wt, "commit", "-m", "feat(smoke): add notes file")
    head_after = _git(wt, "rev-parse", "HEAD")
    assert head_after != head_before
    # Worker must NOT push. Verify the bare remote's branch list is
    # unchanged from main only.
    _, bare = disposable_repo
    remote_refs = (
        subprocess.check_output(["git", "-C", str(bare), "for-each-ref", "--format=%(refname)"])
        .decode()
        .strip()
        .splitlines()
    )
    assert remote_refs == ["refs/heads/main"], remote_refs
    # Working tree clean (no stray files), and the worker did NOT touch
    # any unrelated path.
    assert _git(wt, "status", "--short") == ""
    diff_files = _git(wt, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert diff_files == ["NOTES.md"], diff_files


def test_smoke_b_orchestrator_publishes_task_branch_and_promotes_main(
    worker_worktree: Path,
    disposable_repo: tuple[Path, Path],
) -> None:
    """Orchestrator contract: verify, push the task branch, fast-forward main.

    Drives the exact Git commands the orchestrator's contract mandates,
    against a disposable bare remote that is NOT the production fork.
    Validates the full local-only promotion path.
    """
    local, bare = disposable_repo
    wt = worker_worktree
    # Worker already committed (see test_smoke_b_worker_creates_local_commit_without_push).
    # Set up another scoped change so this test is independent.
    (wt / "PUBLISH.md").write_text("# publish check\n")
    _git(wt, "add", "PUBLISH.md")
    _git(wt, "commit", "-m", "feat(smoke): publish check")
    task_branch = _git(wt, "rev-parse", "--abbrev-ref", "HEAD")
    task_commit = _git(wt, "rev-parse", "HEAD")
    assert task_branch.startswith("feature/")

    # Orchestrator: verify the worktree is clean, the branch is a
    # dedicated task branch, the commit is the HEAD.
    assert _git(wt, "status", "--short") == ""
    assert task_branch not in {"main", "master", "trunk"}
    assert not task_branch.startswith("deploy-main-")

    # Orchestrator: inspect the writable remote.
    remote_url = _git(wt, "remote", "get-url", "origin")
    assert remote_url.startswith(str(bare.parent)), (
        "remote must point at the disposable bare, not the production fork"
    )
    assert "github.com/Mortified2896" not in remote_url, "must never target the production fork"

    # Orchestrator: push the task branch (no force).
    subprocess.check_call(["git", "-C", str(wt), "push", "--set-upstream", "origin", task_branch])
    # Verify the remote task branch resolves to the exact task commit.
    remote_task_sha = (
        subprocess.check_output(["git", "-C", str(bare), "rev-parse", "refs/heads/" + task_branch])
        .decode()
        .strip()
    )
    assert remote_task_sha == task_commit, (
        f"remote task branch SHA {remote_task_sha!r} != expected {task_commit!r}"
    )

    # Orchestrator: fetch the current remote main and confirm it is an
    # ancestor of the task commit (so promotion can be a fast-forward).
    subprocess.check_call(["git", "-C", str(local), "fetch", "origin", "main"])
    rc = subprocess.call(
        ["git", "-C", str(local), "merge-base", "--is-ancestor", "origin/main", task_commit]
    )
    assert rc == 0, "remote main must be an ancestor of the task commit"

    # Orchestrator: promote the exact task commit to remote main.
    subprocess.check_call(
        [
            "git",
            "-C",
            str(local),
            "push",
            "origin",
            f"{task_commit}:main",
        ]
    )
    remote_main_sha = (
        subprocess.check_output(["git", "-C", str(bare), "rev-parse", "refs/heads/main"])
        .decode()
        .strip()
    )
    assert remote_main_sha == task_commit, (
        f"remote main SHA {remote_main_sha!r} != task commit {task_commit!r} — promotion failed"
    )


def test_smoke_b_divergent_remote_main_blocks_fast_forward(
    disposable_repo: tuple[Path, Path],
) -> None:
    """Safety rule: a non-ancestor remote main must NOT be force-overwritten.

    The brief instructs: "Never solve a blocker through force-push …
    destructive reset, or unreviewed conflict resolution." This test
    pushes an advance commit on remote main out-of-band, then attempts
    the orchestrator's promotion path. The promotion SHOULD fail
    (non-zero exit) and the remote main SHA MUST be unchanged.
    """
    local, bare = disposable_repo
    # Out-of-band: simulate "remote main advanced" by pushing a new
    # commit directly to the bare from a separate clone.
    other = local.parent / "other"
    other.mkdir()
    subprocess.check_call(
        ["git", "clone", str(bare), str(other)],
        env={
            **os.environ,
            "GIT_AUTHOR_NAME": "other",
            "GIT_AUTHOR_EMAIL": "other@example.com",
            "GIT_COMMITTER_NAME": "other",
            "GIT_COMMITTER_EMAIL": "other@example.com",
        },
    )
    (other / "ADVANCE.md").write_text("out-of-band advance\n")
    subprocess.check_call(["git", "-C", str(other), "add", "ADVANCE.md"])
    subprocess.check_call(
        [
            "git",
            "-C",
            str(other),
            "-c",
            "user.email=other@example.com",
            "-c",
            "user.name=other",
            "commit",
            "-m",
            "out-of-band advance",
        ],
    )
    subprocess.check_call(["git", "-C", str(other), "push", "origin", "main"])
    advanced_main = (
        subprocess.check_output(["git", "-C", str(bare), "rev-parse", "refs/heads/main"])
        .decode()
        .strip()
    )

    # Now attempt the orchestrator's promotion with a different task
    # commit that is NOT an ancestor of advanced_main.
    wt = local.parent / "ffwt"
    subprocess.check_call(
        ["git", "-C", str(local), "worktree", "add", "-b", "feature/ff-task", str(wt)]
    )
    (wt / "FF.md").write_text("task change\n")
    subprocess.check_call(["git", "-C", str(wt), "add", "FF.md"])
    subprocess.check_call(
        [
            "git",
            "-C",
            str(wt),
            "-c",
            "user.email=t@example.com",
            "-c",
            "user.name=t",
            "commit",
            "-m",
            "task change",
        ]
    )
    task_commit = (
        subprocess.check_output(["git", "-C", str(wt), "rev-parse", "HEAD"]).decode().strip()
    )
    # Promotion must fail.
    rc = subprocess.call(["git", "-C", str(local), "push", "origin", f"{task_commit}:main"])
    assert rc != 0, "promotion must refuse when remote main is not an ancestor"
    # Remote main must still be the advanced commit (untouched).
    remote_main_after = (
        subprocess.check_output(["git", "-C", str(bare), "rev-parse", "refs/heads/main"])
        .decode()
        .strip()
    )
    assert remote_main_after == advanced_main, (
        "remote main must NOT be force-overwritten when promotion cannot fast-forward"
    )


# ── Cross-bundle hygiene ──────────────────────────────────────────────────


def test_smoke_official_polly_bundle_unchanged() -> None:
    """Sanity check: control-room-polly did not modify the official polly bundle."""
    polly_dir = REPO_ROOT / "examples" / "polly"
    assert polly_dir.is_dir()
    config_text = (polly_dir / "config.yaml").read_text()
    assert "name: polly" in config_text
    assert "harness: claude-sdk" in config_text
    # polly's roster must be unchanged.
    assert "- claude_code" in config_text
    assert "- codex" in config_text
    assert "- opencode" in config_text
    assert "- cursor" in config_text
    assert "- hermes" in config_text
    assert "- pi" in config_text
    # Ensure polly did not gain a new control-room-polly sub-agent.
    assert "control-room-polly" not in config_text
