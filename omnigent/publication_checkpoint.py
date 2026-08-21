"""Durable, fail-closed publication checkpoints for development tasks.

This module deliberately does not publish anything. Trusted workers may use Git and
GitHub directly, while a future controller may perform the same operations. The
contract here records progress durably, stops unsafe phase transitions, and reads
the resulting local branch, remote branch, and pull request back independently.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import Protocol


class PublicationState(StrEnum):
    ACTIVE = "active"
    CHECKPOINT_REQUIRED = "checkpoint_required"
    CHECKPOINTED = "checkpointed"
    COMPLETED = "completed"
    BLOCKED_PUBLICATION = "blocked_publication"
    FAILED = "failed"
    CANCELLED = "cancelled"


class GuardedPhase(StrEnum):
    BROAD_VALIDATION = "broad_validation"
    INDEPENDENT_REVIEW = "independent_review"
    ARCHITECTURE_EXPANSION = "architecture_expansion"
    SECOND_ISSUE = "second_issue"


class PublicationCheckpointError(ValueError):
    """Raised when publication evidence or a task transition is unsafe."""


@dataclass(frozen=True)
class PublicationThresholds:
    elapsed_seconds: int | None = 30 * 60
    tool_calls: int | None = 100
    context_tokens: int | None = None


@dataclass(frozen=True)
class PublicationMeasurements:
    elapsed_seconds: int | None
    tool_calls: int | None
    context_tokens: int | None


@dataclass(frozen=True)
class PullRequestReadback:
    url: str
    head: str
    base: str
    draft: bool


@dataclass(frozen=True)
class PublicationCapabilities:
    local_git: bool
    remote_git: bool
    github: bool

    @property
    def ready(self) -> bool:
        return self.local_git and self.remote_git and self.github


@dataclass(frozen=True)
class DirtyPathState:
    task_owned: tuple[str, ...] = ()
    preexisting: tuple[str, ...] = ()

    @property
    def task_worktree_clean(self) -> bool:
        return not self.task_owned


@dataclass(frozen=True)
class PublicationEvidence:
    local_commit: str | None = None
    remote_commit: str | None = None
    pr_url: str | None = None
    pr_head: str | None = None
    pr_base: str | None = None
    pr_draft: bool | None = None
    expected_pr_url: str | None = None
    expected_pr_base: str | None = None
    worktree_clean: bool = False
    task_changes_exist: bool = False
    publication_error: str | None = None
    capabilities: PublicationCapabilities | None = None


@dataclass(frozen=True)
class PublicationRun:
    run_id: str
    issue_key: str
    branch: str
    base_branch: str
    started_at: float
    status: PublicationState = PublicationState.ACTIVE
    last_checkpoint_at: float | None = None
    local_commit: str | None = None
    remote_commit: str | None = None
    pr_url: str | None = None
    pr_head: str | None = None
    pr_base: str | None = None
    publication_error: str | None = None
    capability_preflight: PublicationCapabilities | None = None
    elapsed_seconds: int | None = 0
    tool_calls: int | None = 0
    context_tokens: int | None = None
    checkpoint_elapsed_seconds: int = 0
    checkpoint_tool_calls: int = 0
    checkpoint_context_tokens: int = 0
    checkpoint_reasons: tuple[str, ...] = ()
    baseline_dirty_paths: tuple[str, ...] = ()
    task_owned_paths: tuple[str, ...] = ()
    task_owned_dirty_paths: tuple[str, ...] = ()
    coherent_slice_ready: bool = False
    schema_version: int = field(default=1, init=False)

    @property
    def remotely_checkpointed(self) -> bool:
        return (
            self.local_commit is not None
            and self.local_commit == self.remote_commit == self.pr_head
            and bool(self.pr_url and self.pr_base)
            and self.status in {PublicationState.CHECKPOINTED, PublicationState.COMPLETED}
        )

    @property
    def has_task_owned_changes(self) -> bool:
        return bool(self.task_owned_dirty_paths or self.coherent_slice_ready)


class PublicationReadback(Protocol):
    """Read-only boundary implemented by trusted workers or future controllers."""

    def capability_preflight(self) -> PublicationCapabilities: ...

    def read_local_head(self) -> str: ...

    def read_remote_head(self, branch: str) -> str | None: ...

    def read_pull_request(self, pr_url: str) -> PullRequestReadback | None: ...

    def read_dirty_paths(self) -> tuple[str, ...]: ...


def _paths(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(sorted({value.strip() for value in values if value.strip()}))


def classify_dirty_paths(
    baseline_dirty_paths: Iterable[str],
    current_dirty_paths: Iterable[str],
    *,
    explicitly_task_owned: Iterable[str] = (),
) -> DirtyPathState:
    """Separate new task dirt from unrelated paths that were dirty at start.

    A pre-existing dirty path remains unrelated unless ownership is explicit. This
    prevents a broad ``git add`` from silently absorbing another task's work.
    """
    baseline = set(_paths(baseline_dirty_paths))
    current = set(_paths(current_dirty_paths))
    explicit = set(_paths(explicitly_task_owned))
    task_owned = (current - baseline) | (current & explicit)
    preexisting = (current & baseline) - explicit
    return DirtyPathState(_paths(task_owned), _paths(preexisting))


def checkpoint_reasons(
    measurements: PublicationMeasurements,
    thresholds: PublicationThresholds,
    *,
    since: PublicationMeasurements | None = None,
) -> tuple[str, ...]:
    """Return crossed thresholds, failing closed when a configured metric is absent."""
    reasons: list[str] = []
    for name in ("elapsed_seconds", "tool_calls", "context_tokens"):
        threshold = getattr(thresholds, name)
        if threshold is None:
            continue
        value = getattr(measurements, name)
        if value is None:
            reasons.append(f"{name}_unavailable")
        elif value - (getattr(since, name) or 0 if since else 0) >= threshold:
            reasons.append(f"{name}_threshold")
    return tuple(reasons)


def record_progress(
    run: PublicationRun,
    measurements: PublicationMeasurements,
    thresholds: PublicationThresholds,
    dirty_paths: DirtyPathState,
    *,
    coherent_slice_ready: bool | None = None,
) -> PublicationRun:
    """Update counters and require preservation before more work when needed."""
    reasons = checkpoint_reasons(
        measurements,
        thresholds,
        since=PublicationMeasurements(
            run.checkpoint_elapsed_seconds,
            run.checkpoint_tool_calls,
            run.checkpoint_context_tokens,
        ),
    )
    owns_changes = bool(dirty_paths.task_owned) or bool(coherent_slice_ready)
    status = run.status
    if reasons and owns_changes:
        status = PublicationState.CHECKPOINT_REQUIRED
    elif owns_changes and run.status is PublicationState.CHECKPOINTED:
        status = PublicationState.ACTIVE
    return replace(
        run,
        status=status,
        elapsed_seconds=measurements.elapsed_seconds,
        tool_calls=measurements.tool_calls,
        context_tokens=measurements.context_tokens,
        checkpoint_reasons=reasons if owns_changes else (),
        task_owned_paths=_paths((*run.task_owned_paths, *dirty_paths.task_owned)),
        task_owned_dirty_paths=dirty_paths.task_owned,
        coherent_slice_ready=(
            run.coherent_slice_ready if coherent_slice_ready is None else coherent_slice_ready
        ),
    )


def guard_phase_transition(run: PublicationRun, phase: GuardedPhase) -> None:
    """Stop broad work or another issue until useful work is remotely preserved."""
    if run.has_task_owned_changes and not run.remotely_checkpointed:
        raise PublicationCheckpointError(
            f"{phase.value} requires a verified remote checkpoint for {run.issue_key}"
        )


def guard_issue_transition(run: PublicationRun, next_issue_key: str) -> None:
    if next_issue_key != run.issue_key:
        guard_phase_transition(run, GuardedPhase.SECOND_ISSUE)


def read_publication_evidence(
    readback: PublicationReadback,
    *,
    branch: str,
    pr_url: str,
    worktree_clean: bool,
    publication_error: str | None = None,
    task_changes_exist: bool = False,
    capabilities: PublicationCapabilities | None = None,
    expected_pr_base: str | None = None,
) -> PublicationEvidence:
    """Perform independent local, remote, and PR reads; never trust a worker summary."""
    capabilities = capabilities or readback.capability_preflight()
    local_commit = readback.read_local_head()
    remote_commit = readback.read_remote_head(branch)
    pull = readback.read_pull_request(pr_url)
    return PublicationEvidence(
        local_commit=local_commit,
        remote_commit=remote_commit,
        pr_url=pull.url if pull else pr_url,
        pr_head=pull.head if pull else None,
        pr_base=pull.base if pull else None,
        pr_draft=pull.draft if pull else None,
        expected_pr_url=pr_url,
        expected_pr_base=expected_pr_base,
        worktree_clean=worktree_clean,
        task_changes_exist=task_changes_exist,
        publication_error=publication_error,
        capabilities=capabilities,
    )


def _read_task_dirty_state(run: PublicationRun, readback: PublicationReadback) -> DirtyPathState:
    return classify_dirty_paths(
        run.baseline_dirty_paths,
        readback.read_dirty_paths(),
        explicitly_task_owned=run.task_owned_paths,
    )


def _validate_checkpoint_evidence(evidence: PublicationEvidence) -> None:
    required = {
        "local_commit": evidence.local_commit,
        "remote_commit": evidence.remote_commit,
        "pr_url": evidence.pr_url,
        "pr_head": evidence.pr_head,
        "pr_base": evidence.pr_base,
    }
    missing = [name for name, value in required.items() if not value]
    if missing:
        raise PublicationCheckpointError(f"publication evidence missing: {', '.join(missing)}")
    if (
        evidence.local_commit != evidence.remote_commit
        or evidence.pr_head != evidence.remote_commit
    ):
        raise PublicationCheckpointError("local, remote, and PR heads do not match")
    if evidence.pr_draft is False:
        raise PublicationCheckpointError("publication checkpoint requires a draft PR")
    if evidence.expected_pr_url and evidence.pr_url != evidence.expected_pr_url:
        raise PublicationCheckpointError("PR read-back URL does not match the requested PR")
    if evidence.expected_pr_base and evidence.pr_base != evidence.expected_pr_base:
        raise PublicationCheckpointError("PR base does not match the task branch contract")


def finalize_publication(
    claimed_state: PublicationState,
    evidence: PublicationEvidence,
) -> PublicationState:
    """Validate a terminal/checkpoint state using independently read evidence."""
    if claimed_state is PublicationState.BLOCKED_PUBLICATION:
        if evidence.local_commit is None or not evidence.publication_error:
            raise PublicationCheckpointError(
                "blocked_publication requires a local commit and exact publication error"
            )
        if evidence.capabilities is None:
            raise PublicationCheckpointError("blocked_publication requires capability preflight")
        if not evidence.worktree_clean:
            raise PublicationCheckpointError(
                "blocked_publication requires all task changes in the local commit"
            )
        return claimed_state
    if claimed_state in {PublicationState.FAILED, PublicationState.CANCELLED}:
        if not evidence.worktree_clean:
            raise PublicationCheckpointError(
                f"{claimed_state.value} cannot leave uncommitted task changes"
            )
        if evidence.task_changes_exist and evidence.local_commit is None:
            raise PublicationCheckpointError(
                f"{claimed_state.value} with task changes requires a preserved local commit"
            )
        return claimed_state
    if claimed_state not in {PublicationState.CHECKPOINTED, PublicationState.COMPLETED}:
        raise PublicationCheckpointError(f"{claimed_state.value} is not a finalizable state")
    _validate_checkpoint_evidence(evidence)
    if not evidence.worktree_clean:
        raise PublicationCheckpointError(
            f"{claimed_state.value} requires a clean task-owned worktree"
        )
    return claimed_state


def reconcile_publication(
    run: PublicationRun,
    readback: PublicationReadback,
    *,
    pr_url: str,
    now: float | None = None,
    publication_error: str | None = None,
) -> PublicationRun:
    """Resolve successful publication even when its command acknowledgement was lost."""
    capabilities = PublicationCapabilities(False, False, False)
    dirty = DirtyPathState(task_owned=run.task_owned_dirty_paths)
    try:
        capabilities = readback.capability_preflight()
        dirty = _read_task_dirty_state(run, readback)
        evidence = read_publication_evidence(
            readback,
            branch=run.branch,
            pr_url=pr_url,
            worktree_clean=dirty.task_worktree_clean,
            publication_error=publication_error,
            task_changes_exist=run.has_task_owned_changes,
            capabilities=capabilities,
            expected_pr_base=run.base_branch,
        )
        finalize_publication(PublicationState.CHECKPOINTED, evidence)
    except (OSError, subprocess.SubprocessError, PublicationCheckpointError) as exc:
        local_commit = run.local_commit
        with suppress(OSError, subprocess.SubprocessError):
            local_commit = readback.read_local_head()
        exact_error = publication_error or f"readback failed: {exc}"
        if local_commit is None or not dirty.task_worktree_clean:
            return replace(
                run,
                status=PublicationState.CHECKPOINT_REQUIRED,
                local_commit=local_commit,
                publication_error=exact_error,
                capability_preflight=capabilities,
                task_owned_dirty_paths=dirty.task_owned,
            )
        return replace(
            run,
            status=PublicationState.BLOCKED_PUBLICATION,
            local_commit=local_commit,
            publication_error=exact_error,
            capability_preflight=capabilities,
        )
    return replace(
        run,
        status=PublicationState.CHECKPOINTED,
        last_checkpoint_at=time.time() if now is None else now,
        local_commit=evidence.local_commit,
        remote_commit=evidence.remote_commit,
        pr_url=evidence.pr_url,
        pr_head=evidence.pr_head,
        pr_base=evidence.pr_base,
        publication_error=None,
        capability_preflight=capabilities,
        checkpoint_reasons=(),
        checkpoint_elapsed_seconds=run.elapsed_seconds or 0,
        checkpoint_tool_calls=run.tool_calls or 0,
        checkpoint_context_tokens=run.context_tokens or 0,
        task_owned_dirty_paths=(),
        coherent_slice_ready=False,
    )


def complete_run(run: PublicationRun, readback: PublicationReadback) -> PublicationRun:
    """Re-read every head immediately before allowing ``completed``."""
    if not run.pr_url:
        raise PublicationCheckpointError("completed requires a recorded PR URL")
    evidence = read_publication_evidence(
        readback,
        branch=run.branch,
        pr_url=run.pr_url,
        worktree_clean=_read_task_dirty_state(run, readback).task_worktree_clean,
        task_changes_exist=run.has_task_owned_changes,
        expected_pr_base=run.base_branch,
    )
    finalize_publication(PublicationState.COMPLETED, evidence)
    return replace(
        run,
        status=PublicationState.COMPLETED,
        local_commit=evidence.local_commit,
        remote_commit=evidence.remote_commit,
        pr_head=evidence.pr_head,
        pr_base=evidence.pr_base,
        capability_preflight=evidence.capabilities,
    )


def terminate_run(
    run: PublicationRun,
    claimed_state: PublicationState,
    readback: PublicationReadback,
) -> PublicationRun:
    """Record a truthful failure/cancellation without discarding useful work."""
    if claimed_state not in {PublicationState.FAILED, PublicationState.CANCELLED}:
        raise PublicationCheckpointError("terminate_run only accepts failed or cancelled")
    dirty = _read_task_dirty_state(run, readback)
    local_commit = readback.read_local_head() if run.has_task_owned_changes else run.local_commit
    finalize_publication(
        claimed_state,
        PublicationEvidence(
            local_commit=local_commit,
            worktree_clean=dirty.task_worktree_clean,
            task_changes_exist=run.has_task_owned_changes,
        ),
    )
    return replace(
        run,
        status=claimed_state,
        local_commit=local_commit,
        task_owned_dirty_paths=dirty.task_owned,
    )


class PublicationRunStore:
    """Atomic JSON persistence for the small controller-neutral run record."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)

    def load(self) -> PublicationRun | None:
        if not self.path.exists():
            return None
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        if raw.get("schema_version") != 1:
            raise PublicationCheckpointError("unsupported publication run schema")
        raw["status"] = PublicationState(raw["status"])
        raw["capability_preflight"] = (
            PublicationCapabilities(**raw["capability_preflight"])
            if raw.get("capability_preflight")
            else None
        )
        for name in (
            "checkpoint_reasons",
            "baseline_dirty_paths",
            "task_owned_paths",
            "task_owned_dirty_paths",
        ):
            raw[name] = tuple(raw.get(name, ()))
        raw.pop("schema_version", None)
        return PublicationRun(**raw)

    def save(self, run: PublicationRun) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(asdict(run), sort_keys=True, indent=2) + "\n"
        fd, temporary_name = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)

    def start(self, run: PublicationRun) -> None:
        existing = self.load()
        if existing:
            if existing.run_id == run.run_id:
                return
            if existing.issue_key != run.issue_key:
                guard_issue_transition(existing, run.issue_key)
            elif existing.has_task_owned_changes and not existing.remotely_checkpointed:
                raise PublicationCheckpointError(
                    f"existing run {existing.run_id} has unpublished task work"
                )
        self.save(run)

    def require(self) -> PublicationRun:
        run = self.load()
        if run is None:
            raise PublicationCheckpointError("publication run has not been started")
        return run


class PublicationCheckpointController:
    """Persist every enforcement decision without owning publication credentials."""

    def __init__(
        self,
        store: PublicationRunStore,
        readback: PublicationReadback,
        *,
        thresholds: PublicationThresholds | None = None,
    ) -> None:
        self.store = store
        self.readback = readback
        self.thresholds = thresholds or PublicationThresholds()

    def begin(
        self,
        *,
        run_id: str,
        issue_key: str,
        branch: str,
        base_branch: str,
        started_at: float | None = None,
    ) -> PublicationRun:
        run = PublicationRun(
            run_id=run_id,
            issue_key=issue_key,
            branch=branch,
            base_branch=base_branch,
            started_at=time.time() if started_at is None else started_at,
            baseline_dirty_paths=_paths(self.readback.read_dirty_paths()),
        )
        self.store.start(run)
        return run

    def observe(
        self,
        measurements: PublicationMeasurements,
        *,
        coherent_slice_ready: bool = False,
        explicitly_task_owned: Iterable[str] = (),
    ) -> PublicationRun:
        run = self.store.require()
        dirty = classify_dirty_paths(
            run.baseline_dirty_paths,
            self.readback.read_dirty_paths(),
            explicitly_task_owned=(*run.task_owned_paths, *explicitly_task_owned),
        )
        updated = record_progress(
            run,
            measurements,
            self.thresholds,
            dirty,
            coherent_slice_ready=coherent_slice_ready,
        )
        self.store.save(updated)
        return updated

    def enter_phase(self, phase: GuardedPhase) -> None:
        guard_phase_transition(self.store.require(), phase)

    def checkpoint(
        self,
        *,
        pr_url: str,
        now: float | None = None,
        publication_error: str | None = None,
    ) -> PublicationRun:
        updated = reconcile_publication(
            self.store.require(),
            self.readback,
            pr_url=pr_url,
            now=now,
            publication_error=publication_error,
        )
        self.store.save(updated)
        return updated

    def complete(self) -> PublicationRun:
        updated = complete_run(self.store.require(), self.readback)
        self.store.save(updated)
        return updated

    def terminate(self, state: PublicationState) -> PublicationRun:
        updated = terminate_run(self.store.require(), state, self.readback)
        self.store.save(updated)
        return updated


CommandRunner = Callable[[tuple[str, ...], Path], str]


def _run_readonly(command: tuple[str, ...], cwd: Path) -> str:
    result = subprocess.run(
        command,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    return result.stdout.strip()


class GitHubCliPublicationReadback:
    """Read-only Git/``gh`` adapter; it never commits, pushes, or edits a PR."""

    def __init__(
        self,
        repository: str | Path,
        *,
        remote: str = "origin",
        command_runner: CommandRunner = _run_readonly,
    ) -> None:
        self.repository = Path(repository)
        self.remote = remote
        self._run = command_runner

    def capability_preflight(self) -> PublicationCapabilities:
        def available(command: tuple[str, ...]) -> bool:
            try:
                self._run(command, self.repository)
            except (OSError, subprocess.SubprocessError):
                return False
            return True

        return PublicationCapabilities(
            local_git=available(("git", "rev-parse", "--git-dir")),
            remote_git=available(("git", "remote", "get-url", self.remote)),
            github=available(("gh", "auth", "status")),
        )

    def read_local_head(self) -> str:
        return self._run(("git", "rev-parse", "HEAD"), self.repository)

    def read_remote_head(self, branch: str) -> str | None:
        output = self._run(
            ("git", "ls-remote", "--heads", self.remote, f"refs/heads/{branch}"),
            self.repository,
        )
        return output.split()[0] if output else None

    def read_dirty_paths(self) -> tuple[str, ...]:
        output = self._run(
            ("git", "status", "--porcelain=v1", "-z", "--untracked-files=all"),
            self.repository,
        )
        records = output.split("\0")
        paths: list[str] = []
        index = 0
        while index < len(records):
            record = records[index]
            index += 1
            if not record:
                continue
            status, path = record[:2], record[3:]
            paths.append(path)
            if ("R" in status or "C" in status) and index < len(records):
                prior_path = records[index]
                index += 1
                if prior_path:
                    paths.append(prior_path)
        return _paths(paths)

    def read_pull_request(self, pr_url: str) -> PullRequestReadback | None:
        output = self._run(
            (
                "gh",
                "pr",
                "view",
                pr_url,
                "--json",
                "url,headRefOid,baseRefName,isDraft",
            ),
            self.repository,
        )
        loaded: object = json.loads(output)
        if not isinstance(loaded, dict):
            raise PublicationCheckpointError("GitHub PR read-back was not an object")
        url = loaded.get("url")
        head = loaded.get("headRefOid")
        base = loaded.get("baseRefName")
        draft = loaded.get("isDraft")
        if (
            not isinstance(url, str)
            or not url
            or not isinstance(head, str)
            or not head
            or not isinstance(base, str)
            or not base
        ):
            raise PublicationCheckpointError("GitHub PR read-back is missing identity fields")
        if not isinstance(draft, bool):
            raise PublicationCheckpointError("GitHub PR read-back is missing draft state")
        return PullRequestReadback(
            url=url,
            head=head,
            base=base,
            draft=draft,
        )
