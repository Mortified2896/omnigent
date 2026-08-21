"""Publication checkpoint enforcement and recovery matrix."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

from omnigent.publication_checkpoint import (
    DirtyPathState,
    GitHubCliPublicationReadback,
    GuardedPhase,
    PublicationCapabilities,
    PublicationCheckpointError,
    PublicationEvidence,
    PublicationMeasurements,
    PublicationRun,
    PublicationRunStore,
    PublicationState,
    PublicationThresholds,
    PullRequestReadback,
    checkpoint_reasons,
    classify_dirty_paths,
    complete_run,
    finalize_publication,
    guard_issue_transition,
    guard_phase_transition,
    read_publication_evidence,
    reconcile_publication,
    record_progress,
)

SHA = "a" * 40
OTHER_SHA = "b" * 40
PR_URL = "https://github.com/example/repo/pull/1"
CAPABILITIES = PublicationCapabilities(True, True, True)


def _run(**changes: object) -> PublicationRun:
    run = PublicationRun(
        run_id="run-1",
        issue_key="example/repo#118",
        branch="codex/issue-118",
        base_branch="main",
        started_at=1.0,
    )
    return replace(run, **changes)


class FakeReadback:
    def __init__(
        self,
        *,
        local: str = SHA,
        remote: str | None = SHA,
        pr: PullRequestReadback | None = None,
        capabilities: PublicationCapabilities = CAPABILITIES,
    ) -> None:
        self.local = local
        self.remote = remote
        self.pr = pr or PullRequestReadback(PR_URL, SHA, "main", True)
        self.capabilities = capabilities
        self.calls: list[str] = []

    def capability_preflight(self) -> PublicationCapabilities:
        self.calls.append("preflight")
        return self.capabilities

    def read_local_head(self) -> str:
        self.calls.append("local")
        return self.local

    def read_remote_head(self, branch: str) -> str | None:
        self.calls.append(f"remote:{branch}")
        return self.remote

    def read_pull_request(self, pr_url: str) -> PullRequestReadback | None:
        self.calls.append(f"pr:{pr_url}")
        return self.pr


def test_false_completion_without_remote_evidence_is_rejected() -> None:
    with pytest.raises(PublicationCheckpointError, match="remote_commit"):
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(local_commit=SHA, worktree_clean=True),
        )


def test_remote_head_move_is_rejected() -> None:
    with pytest.raises(PublicationCheckpointError, match="do not match"):
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(
                local_commit=SHA,
                remote_commit=OTHER_SHA,
                pr_head=OTHER_SHA,
                pr_url=PR_URL,
                pr_base="main",
                pr_draft=True,
                worktree_clean=True,
            ),
        )


def test_push_failure_preserves_blocked_publication() -> None:
    assert (
        finalize_publication(
            PublicationState.BLOCKED_PUBLICATION,
            PublicationEvidence(
                local_commit=SHA,
                publication_error="git push: rejected",
                capabilities=PublicationCapabilities(True, False, True),
            ),
        )
        is PublicationState.BLOCKED_PUBLICATION
    )


def test_blocked_publication_requires_capability_preflight() -> None:
    with pytest.raises(PublicationCheckpointError, match="capability preflight"):
        finalize_publication(
            PublicationState.BLOCKED_PUBLICATION,
            PublicationEvidence(local_commit=SHA, publication_error="network timeout"),
        )


def test_matching_readback_allows_completion() -> None:
    assert (
        finalize_publication(
            PublicationState.COMPLETED,
            PublicationEvidence(
                local_commit=SHA,
                remote_commit=SHA,
                pr_head=SHA,
                pr_url=PR_URL,
                pr_base="main",
                pr_draft=True,
                worktree_clean=True,
            ),
        )
        is PublicationState.COMPLETED
    )


def test_non_draft_pr_is_not_a_checkpoint() -> None:
    with pytest.raises(PublicationCheckpointError, match="draft PR"):
        finalize_publication(
            PublicationState.CHECKPOINTED,
            PublicationEvidence(
                local_commit=SHA,
                remote_commit=SHA,
                pr_head=SHA,
                pr_url=PR_URL,
                pr_base="main",
                pr_draft=False,
                worktree_clean=True,
            ),
        )


def test_exact_eight_hour_dirty_budget_pattern_requires_checkpoint() -> None:
    run = record_progress(
        _run(),
        PublicationMeasurements(8 * 60 * 60, 4000, 5_000_000),
        PublicationThresholds(30 * 60, 100, 200_000),
        DirtyPathState(task_owned=("omnigent/useful.py",)),
        coherent_slice_ready=True,
    )
    assert run.status is PublicationState.CHECKPOINT_REQUIRED
    assert run.checkpoint_reasons == (
        "elapsed_seconds_threshold",
        "tool_calls_threshold",
        "context_tokens_threshold",
    )
    with pytest.raises(PublicationCheckpointError, match="broad_validation"):
        guard_phase_transition(run, GuardedPhase.BROAD_VALIDATION)


def test_coherent_slice_must_be_published_before_broad_tests() -> None:
    run = _run(coherent_slice_ready=True)
    with pytest.raises(PublicationCheckpointError, match="broad_validation"):
        guard_phase_transition(run, GuardedPhase.BROAD_VALIDATION)


def test_configured_unavailable_measurement_fails_closed() -> None:
    assert checkpoint_reasons(
        PublicationMeasurements(10, 2, None), PublicationThresholds(None, None, 1000)
    ) == ("context_tokens_unavailable",)


def test_no_changes_do_not_trigger_checkpoint_hard_stop() -> None:
    run = record_progress(
        _run(),
        PublicationMeasurements(3600, 200, None),
        PublicationThresholds(100, 10, 1000),
        DirtyPathState(),
    )
    assert run.status is PublicationState.ACTIVE
    assert run.checkpoint_reasons == ()


def test_preexisting_dirty_paths_are_not_claimed_by_task() -> None:
    state = classify_dirty_paths(
        ["user-notes.md", "shared.py"],
        ["user-notes.md", "shared.py", "omnigent/new.py"],
    )
    assert state.task_owned == ("omnigent/new.py",)
    assert state.preexisting == ("shared.py", "user-notes.md")


def test_preexisting_path_requires_explicit_task_ownership() -> None:
    state = classify_dirty_paths(["shared.py"], ["shared.py"], explicitly_task_owned=["shared.py"])
    assert state.task_owned == ("shared.py",)
    assert state.preexisting == ()


def test_second_issue_is_blocked_until_remote_checkpoint() -> None:
    with pytest.raises(PublicationCheckpointError, match="second_issue"):
        guard_issue_transition(_run(coherent_slice_ready=True), "example/repo#119")


def test_sequential_issue_allowed_after_verified_checkpoint() -> None:
    run = _run(
        status=PublicationState.CHECKPOINTED,
        local_commit=SHA,
        remote_commit=SHA,
        pr_url=PR_URL,
        pr_head=SHA,
        pr_base="main",
    )
    guard_issue_transition(run, "example/repo#119")


def test_readback_is_independent_and_complete() -> None:
    reader = FakeReadback()
    evidence = read_publication_evidence(
        reader, branch="codex/issue-118", pr_url=PR_URL, worktree_clean=True
    )
    assert evidence.local_commit == evidence.remote_commit == evidence.pr_head == SHA
    assert reader.calls == [
        "preflight",
        "local",
        "remote:codex/issue-118",
        f"pr:{PR_URL}",
    ]


def test_pr_acknowledgement_loss_reconciles_from_readback() -> None:
    result = reconcile_publication(
        _run(local_commit=SHA),
        FakeReadback(),
        pr_url=PR_URL,
        now=123.0,
        publication_error="POST /pulls timed out before acknowledgement",
    )
    assert result.status is PublicationState.CHECKPOINTED
    assert result.last_checkpoint_at == 123.0
    assert result.publication_error is None
    assert result.local_commit == result.remote_commit == result.pr_head == SHA


def test_push_rejection_becomes_narrow_recoverable_blocker() -> None:
    result = reconcile_publication(
        _run(local_commit=SHA),
        FakeReadback(remote=None),
        pr_url=PR_URL,
        publication_error="git push origin branch: rejected",
    )
    assert result.status is PublicationState.BLOCKED_PUBLICATION
    assert result.local_commit == SHA
    assert result.publication_error == "git push origin branch: rejected"
    assert result.capability_preflight == CAPABILITIES


def test_repeated_reconciliation_is_idempotent() -> None:
    reader = FakeReadback()
    first = reconcile_publication(_run(local_commit=SHA), reader, pr_url=PR_URL, now=123.0)
    second = reconcile_publication(first, reader, pr_url=PR_URL, now=123.0)
    assert second == first


def test_complete_run_rechecks_remote_and_pr_heads() -> None:
    run = reconcile_publication(_run(local_commit=SHA), FakeReadback(), pr_url=PR_URL)
    completed = complete_run(run, FakeReadback())
    assert completed.status is PublicationState.COMPLETED


def test_complete_run_rejects_branch_move_after_checkpoint() -> None:
    run = reconcile_publication(_run(local_commit=SHA), FakeReadback(), pr_url=PR_URL)
    with pytest.raises(PublicationCheckpointError, match="do not match"):
        complete_run(run, FakeReadback(remote=OTHER_SHA))


@pytest.mark.parametrize("state", [PublicationState.FAILED, PublicationState.CANCELLED])
def test_truthful_non_success_terminal_state_preserves_dirty_work(
    state: PublicationState,
) -> None:
    with pytest.raises(PublicationCheckpointError, match="preserved local commit"):
        finalize_publication(state, PublicationEvidence(worktree_clean=False))
    assert (
        finalize_publication(state, PublicationEvidence(local_commit=SHA, worktree_clean=False))
        is state
    )


def test_durable_run_round_trip_and_atomic_replacement(tmp_path: Path) -> None:
    store = PublicationRunStore(tmp_path / "publication-run.json")
    run = _run(
        status=PublicationState.CHECKPOINT_REQUIRED,
        checkpoint_reasons=("tool_calls_threshold",),
        baseline_dirty_paths=("notes.md",),
        task_owned_dirty_paths=("omnigent/change.py",),
        capability_preflight=CAPABILITIES,
    )
    store.save(run)
    assert store.load() == run
    assert not list(tmp_path.glob(".publication-run.json.*"))


def test_durable_store_blocks_second_issue_with_unpublished_work(tmp_path: Path) -> None:
    store = PublicationRunStore(tmp_path / "publication-run.json")
    store.start(_run(coherent_slice_ready=True))
    with pytest.raises(PublicationCheckpointError, match="second_issue"):
        store.start(replace(_run(), run_id="run-2", issue_key="example/repo#119"))


def test_corrupt_or_unknown_durable_state_fails_closed(tmp_path: Path) -> None:
    path = tmp_path / "publication-run.json"
    path.write_text(json.dumps({"schema_version": 999}), encoding="utf-8")
    with pytest.raises(PublicationCheckpointError, match="unsupported"):
        PublicationRunStore(path).load()


def test_git_github_adapter_uses_only_read_only_commands(tmp_path: Path) -> None:
    calls: list[tuple[str, ...]] = []

    def command_runner(command: tuple[str, ...], cwd: Path) -> str:
        assert cwd == tmp_path
        calls.append(command)
        if command[:3] == ("git", "rev-parse", "HEAD"):
            return SHA
        if command[:2] == ("git", "ls-remote"):
            return f"{SHA}\trefs/heads/codex/issue-118"
        if command[:3] == ("gh", "pr", "view"):
            return json.dumps(
                {"url": PR_URL, "headRefOid": SHA, "baseRefName": "main", "isDraft": True}
            )
        return "ok"

    adapter = GitHubCliPublicationReadback(tmp_path, command_runner=command_runner)
    evidence = read_publication_evidence(
        adapter, branch="codex/issue-118", pr_url=PR_URL, worktree_clean=True
    )
    assert evidence.local_commit == evidence.remote_commit == evidence.pr_head == SHA
    assert all(command[0:2] not in {("git", "push"), ("gh", "pr-create")} for command in calls)
