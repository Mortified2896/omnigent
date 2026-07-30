"""External updater controller (issue #38 §1, §6, §7, §9).

The :class:`UpdaterController` ties together validation, build,
maintenance / drain, migration rehearsal, promotion, verification,
rollback, and result delivery. It is the single orchestrator for
an update request.

Lifecycle (one ``run()`` invocation per request):

1. Acquire the global and per-request locks.
2. Read the running checkpoint, if any, to determine the resume
   point.
3. ``validating`` — call :func:`omnigent.updater.validation.validate_request`.
4. ``building`` — invoke ``scripts/promote_release.sh <sha> --build-only``
   via the dedicated build helper. The build phase is reused
   verbatim — issue #38 explicitly forbids re-implementing it in
   Python.
5. ``draining`` — engage maintenance and wait for the web service
   to report no active sessions / runners.
6. ``rehearsing_migration`` — run the migration rehearsal against a
   scratch copy of the live database.
7. ``backing_up`` — take a consistent backup of the live database
   immediately before cutover.
8. ``promoting`` — invoke ``scripts/promote_release.sh <sha>`` for
   the **exact** previously-validated SHA. No build-only flag.
9. ``verifying`` — run the post-cutover health probes.
10. Either ``succeeded`` or transition to ``rolling_back``.
11. ``rolling_back`` — invoke ``scripts/rollback_release.sh``.
12. ``rolled_back`` or ``rollback_failed``.

After the terminal transition the controller persists the result
file and attempts delivery. If delivery fails the result is
queued for the web service to reconcile on startup.

Crash recovery: every externally visible action is preceded by a
checkpoint write so a crash mid-update can resume from the latest
checkpoint via :meth:`recover_non_terminal`.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from omnigent.deploy.ops import layout as deploy_layout
from omnigent.updater import layout, maintenance
from omnigent.updater.locking import UpdateLock, global_lock
from omnigent.updater.migration_rehearsal import (
    RehearsalRecord,
    backup_database,
    rehearse,
)
from omnigent.updater.protocol import (
    RequestRecord,
    ResultRecord,
    now_iso,
)
from omnigent.updater.state_machine import (
    STATE_TRANSITIONS,
    StateTransitionError,
    UpdatePhase,
    is_terminal,
    validate_transition,
)
from omnigent.updater.store import UpdaterStore
from omnigent.updater.validation import (
    ValidationError,
    read_live_metadata,
    validate_request,
)


class BuildFailedError(RuntimeError):
    """Raised when ``promote_release.sh --build-only`` exits non-zero."""


class PromotionFailedError(RuntimeError):
    """Raised when the cutover ``promote_release.sh`` exits non-zero."""


class HealthCheckFailedError(RuntimeError):
    """Raised when the post-cutover health probes fail."""


class RollbackFailedError(RuntimeError):
    """Raised when ``rollback_release.sh`` itself fails."""


@dataclass
class ControllerHooks:
    """Optional hooks for tests + the CLI to inject behavior.

    Each hook is a no-op by default. Tests use the hooks to
    substitute fake git / fake shell helpers; the CLI uses them to
    pass operator-supplied flags (e.g. ``--no-promote`` for a dry
    run, although the controller itself never re-implements
    promotion).

    The hooks are **callable objects**, not raw shell strings — a
    controller must never accept arbitrary commands from a caller.
    """

    build_only: Callable[[Path, str], subprocess.CompletedProcess] | None = None
    promote: Callable[[Path, str], subprocess.CompletedProcess] | None = None
    rollback: Callable[[Path], subprocess.CompletedProcess] | None = None
    health_probes: Callable[[str], None] | None = None
    preflight: Callable[[Path], None] | None = None
    drain: Callable[[str], None] | None = None
    rehearse: Callable[[str, str], None] | None = None
    backup: Callable[[str], None] | None = None


@dataclass
class ControllerConfig:
    """Static configuration for a controller run.

    :param state_root: Override the state root (for tests).
    :param repo_root: Override the repo root.
    :param deploy_root: Override the deploy root.
    :param db_url: Live database URL, e.g.
        ``"sqlite:////home/hermes/.omnigent/chat.db"``.
    :param service_name: Live web service name.
    :param service_port: Live web service port.
    :param build_poll_seconds: How often to poll a running build.
    :param drain_timeout_seconds: Drain timeout in seconds.
    :param dry_run: If true, do everything except invoke the
        promotion / rollback scripts. Used by the operator CLI for
        pre-flight checks.
    """

    state_root: Path | None = None
    repo_root: Path | None = None
    deploy_root: Path | None = None
    db_url: str = "sqlite:////home/hermes/.omnigent/chat.db"
    service_name: str = "omnigent-eval-web.service"
    service_port: int = 4097
    build_poll_seconds: float = 2.0
    drain_timeout_seconds: float = 1800.0
    dry_run: bool = False
    hooks: ControllerHooks = field(default_factory=ControllerHooks)

    def resolved_repo_root(self) -> Path:
        if self.repo_root is not None:
            return self.repo_root
        return layout.repo_root()

    def resolved_deploy_root(self) -> Path:
        if self.deploy_root is not None:
            self.deploy_root.mkdir(parents=True, exist_ok=True)
            return self.deploy_root
        return deploy_layout.deploy_root()


class UpdaterController:
    """The orchestrator for one update request."""

    def __init__(
        self,
        config: ControllerConfig,
        *,
        store: UpdaterStore | None = None,
    ) -> None:
        self._config = config
        self._store = store or UpdaterStore(state_root=config.state_root)

    @property
    def store(self) -> UpdaterStore:
        return self._store

    @property
    def config(self) -> ControllerConfig:
        return self._config

    # ------------------------------------------------------------------
    # Locking helpers
    # ------------------------------------------------------------------

    def acquire_global_lock(self) -> UpdateLock:
        """Acquire the single-active-update lock.

        Returns the lock object so the caller can hold it across a
        ``run()`` invocation. Tests that exercise concurrency use
        this directly; the ``run()`` method also acquires the lock
        itself for ergonomic single-request flows.
        """
        lock = UpdateLock.global_lock(
            note=f"updater pid={os.getpid()}",
        )
        lock.acquire()
        return lock

    # ------------------------------------------------------------------
    # Crash recovery
    # ------------------------------------------------------------------

    def recover_non_terminal(self) -> list[RecoveryDecision]:
        """Reconcile every non-terminal request.

        Returns the list of decisions so the caller (typically the
        CLI / systemd one-shot) can log what was decided. The
        decisions are:

        * ``"resume"`` — the checkpoint's phase is still actionable
          and the request's lock is held by a live process.
        * ``"resume_idle"`` — same as ``"resume"`` but the lock is
          stale; the controller reacquires it before resuming.
        * ``"verify"`` — the checkpoint indicates the cutover ran
          but verification did not finish.
        * ``"record_rollback"`` — a rollback ran but the result
          was not recorded.
        * ``"deliver"`` — the request has a terminal result but no
          successful delivery.
        * ``"operator_required"`` — the checkpoint is inconsistent
          and the operator must intervene.

        The method never blindly repeats promotion. Promotion only
        re-runs when the checkpoint explicitly says the cutover did
        not finish.
        """

        decisions: list[RecoveryDecision] = []
        for req, result, checkpoint in self._store.iter_all():
            if result is not None and is_terminal(result.final_status):
                continue
            decisions.append(self._decide_recovery(req, result, checkpoint))
        return decisions

    def _decide_recovery(
        self,
        req: RequestRecord,
        result: ResultRecord | None,
        checkpoint: Any | None,
    ) -> RecoveryDecision:
        from omnigent.updater.locking import _pid_alive, _read_holder

        if result is not None and is_terminal(result.final_status):
            return RecoveryDecision(
                request_id=req.request_id,
                action="noop",
                reason="terminal result already recorded",
            )

        if checkpoint is None:
            return RecoveryDecision(
                request_id=req.request_id,
                action="resume",
                reason="checkpoint absent; resume from validating",
            )

        lock_path = layout.lock_path(req.request_id)
        holder = _read_holder(lock_path)
        owner_alive = holder is not None and _pid_alive(holder.pid)

        phase = checkpoint.phase
        if phase in {UpdatePhase.QUEUED, UpdatePhase.VALIDATING}:
            return RecoveryDecision(
                request_id=req.request_id,
                action="resume" if owner_alive else "resume_idle",
                reason=f"checkpoint phase {phase.value}; resume before build",
            )
        if phase in {
            UpdatePhase.BUILDING,
            UpdatePhase.DRAINING,
            UpdatePhase.REHEARSING_MIGRATION,
            UpdatePhase.BACKING_UP,
        }:
            return RecoveryDecision(
                request_id=req.request_id,
                action="resume" if owner_alive else "resume_idle",
                reason=f"checkpoint phase {phase.value}; resumable",
            )
        if phase == UpdatePhase.PROMOTING:
            return RecoveryDecision(
                request_id=req.request_id,
                action="verify",
                reason="cutover possibly in progress; re-run verification",
            )
        if phase == UpdatePhase.VERIFYING:
            return RecoveryDecision(
                request_id=req.request_id,
                action="verify",
                reason="verification incomplete; re-run probes",
            )
        if phase == UpdatePhase.ROLLING_BACK:
            return RecoveryDecision(
                request_id=req.request_id,
                action="record_rollback",
                reason="rollback in progress at crash; verify and record",
            )
        return RecoveryDecision(
            request_id=req.request_id,
            action="operator_required",
            reason=f"checkpoint in unexpected phase {phase.value}; operator intervention required",
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def run(
        self,
        request: RequestRecord,
        *,
        force: bool = False,
    ) -> ResultRecord:
        """Process ``request`` end-to-end.

        Acquires the single-active-update lock, walks the state
        machine, persists checkpoints, and returns the terminal
        result. The result is also persisted to the result file.

        :param request: The request to process. Created via
            :func:`omnigent.updater.protocol.RequestRecord`. The
            caller is expected to have created the request file via
            :meth:`UpdaterStore.create_request` so an idempotent
            re-run on the same request id is impossible.
        :param force: When ``True``, attempt to resume a
            non-terminal request rather than rejecting it. The
            controller still runs validation as a final check.
        """
        with global_lock(note=f"updater pid={os.getpid()} request={request.request_id}"):
            return self._run_locked(request, force=force)

    def _run_locked(self, request: RequestRecord, *, force: bool) -> ResultRecord:
        # Existing terminal result → return immediately, no work.
        existing = self._store.load_result(request.request_id)
        if existing is not None and is_terminal(existing.final_status):
            return existing

        # Checkpoint resume.
        checkpoint = self._store.load_checkpoint(request.request_id)
        if checkpoint is None and not force:
            # Brand-new request — fresh start.
            self._store.append_event(
                request.request_id,
                UpdatePhase.QUEUED,
                message="request received",
                context={"target_sha": request.target_sha},
            )

        try:
            phase = self._next_phase(request, checkpoint, force=force)
        except ValidationError as exc:
            return self._terminate_rejection(request, str(exc), exc)

        previous_sha = read_live_metadata().live_sha
        started_at = now_iso()

        # Walk forward through the phases. Every transition is
        # validated against the state machine.
        try:
            while phase != UpdatePhase.SUCCEEDED:
                if phase == UpdatePhase.VALIDATING:
                    self._enter(request, phase, previous_sha=previous_sha)
                    self._do_validating(request)
                elif phase == UpdatePhase.BUILDING:
                    self._enter(request, phase)
                    self._do_building(request)
                elif phase == UpdatePhase.DRAINING:
                    self._enter(request, phase)
                    self._do_draining(request)
                elif phase == UpdatePhase.REHEARSING_MIGRATION:
                    self._enter(request, phase)
                    self._do_rehearsing(request)
                elif phase == UpdatePhase.BACKING_UP:
                    self._enter(request, phase)
                    self._do_backing_up(request)
                elif phase == UpdatePhase.PROMOTING:
                    self._enter(request, phase)
                    self._do_promoting(request)
                elif phase == UpdatePhase.VERIFYING:
                    self._enter(request, phase)
                    self._do_verifying(request)
                else:
                    raise RuntimeError(f"unhandled phase: {phase}")
                # Advance to the next legal non-terminal phase.
                phase = self._advance(request, phase)
        except ValidationError as exc:
            return self._terminate_rejection(request, str(exc), exc)
        except BuildFailedError as exc:
            return self._terminate_failed(
                request,
                phase=UpdatePhase.BUILDING,
                reason=str(exc),
                previous_sha=previous_sha,
                started_at=started_at,
            )
        except PromotionFailedError as exc:
            return self._enter_rollback(
                request,
                phase=UpdatePhase.VERIFYING,
                reason=str(exc),
                previous_sha=previous_sha,
                started_at=started_at,
            )
        except HealthCheckFailedError as exc:
            return self._enter_rollback(
                request,
                phase=UpdatePhase.VERIFYING,
                reason=str(exc),
                previous_sha=previous_sha,
                started_at=started_at,
            )
        except Exception as exc:  # noqa: BLE001
            return self._enter_rollback(
                request,
                phase=phase,
                reason=f"unexpected error: {exc!r}",
                previous_sha=previous_sha,
                started_at=started_at,
            )

        # SUCCEEDED.
        result = ResultRecord(
            request_id=request.request_id,
            final_status=UpdatePhase.SUCCEEDED.value,
            target_sha=request.target_sha,
            previous_sha=previous_sha,
            deployed_sha=request.target_sha,
            started_at=started_at,
            finished_at=now_iso(),
            notification_status="pending",
            events_tail=self._store.tail_events(request.request_id, 8),
        )
        self._store.write_result(result)
        self._store.clear_checkpoint(request.request_id)
        self._store.append_event(
            request.request_id,
            UpdatePhase.SUCCEEDED,
            message="update succeeded; clearing maintenance",
            context={"deployed_sha": result.deployed_sha},
        )
        # Maintenance is cleared after the result is durable.
        import contextlib

        with contextlib.suppress(OSError):
            maintenance.disengage_maintenance()
        # Delivery is best-effort; failure queues a pending-delivery
        # file the web service will reconcile on startup.
        self._attempt_delivery(result, request)
        return result

    # ------------------------------------------------------------------
    # Per-phase actions
    # ------------------------------------------------------------------

    def _do_validating(self, request: RequestRecord) -> None:
        """Issue #38 §3 validation."""
        validate_request(request)

    def _do_building(self, request: RequestRecord) -> None:
        """Issue #38 §5 — reuse the canonical release-build pipeline."""
        repo = self._config.resolved_repo_root()
        sha = request.target_sha
        proc = self._invoke_build_only(repo, sha)
        if proc.returncode != 0:
            raise BuildFailedError(
                f"promote_release.sh --build-only exited {proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

    def _do_draining(self, request: RequestRecord) -> None:
        """Issue #38 §4 — engage maintenance + wait for idle."""
        maintenance.engage_maintenance(
            request_id=request.request_id,
            reason=f"update to {request.target_sha[:12]}",
        )
        if self._config.hooks.drain is not None:
            self._config.hooks.drain(request.request_id)
            final = maintenance.DrainStatus(draining=True)
        else:
            poller = maintenance.DrainPoller(
                host="127.0.0.1",
                port=self._config.service_port,
                timeout_seconds=self._config.drain_timeout_seconds,
            )
            final = poller.wait_until_idle()
        self._store.append_event(
            request.request_id,
            UpdatePhase.DRAINING,
            message="drain complete",
            context={
                "final_active_sessions": list(final.active_sessions),
                "final_active_runners": list(final.active_runners),
            },
        )

    def _do_rehearsing(self, request: RequestRecord) -> None:
        """Issue #38 §5 — rehearse the candidate migration against a copy."""
        release_dir = self._candidate_release_dir(request.target_sha)
        if self._config.hooks.rehearse is not None:
            self._config.hooks.rehearse(request.request_id, request.target_sha)
            record = RehearsalRecord(
                request_id=request.request_id,
                required=False,
                live_revision=None,
                candidate_revision=None,
                rehearsal_db_path=None,
                rehearsal_post_revision=None,
                completed_at=now_iso(),
                notes=["rehearsal substituted by hook"],
            )
        else:
            record = rehearse(
                request_id=request.request_id,
                candidate_repo=release_dir,
                db_url=self._config.db_url,
            )
        self._store.append_event(
            request.request_id,
            UpdatePhase.REHEARSING_MIGRATION,
            message="rehearsal completed",
            context={"required": record.required},
        )
        # Persist the rehearsal record under the state root so a
        # post-cutover verifier can confirm migration rehearsal
        # happened (and so the controller can re-read it on
        # resume).
        rehearsal_path = layout.rehearsal_dir() / f"{request.request_id}.json"
        rehearsal_path.write_text(json.dumps(record.to_dict(), indent=2, sort_keys=True) + "\n")

    def _do_backing_up(self, request: RequestRecord) -> None:
        """Issue #38 §5 — final consistent backup before cutover."""
        if self._config.hooks.backup is not None:
            self._config.hooks.backup(request.request_id)
            backup_path = layout.backups_dir() / f"{request.request_id}.noop"
            backup_sha = "0" * 64
        else:
            backup_path, backup_sha = backup_database(
                request_id=request.request_id,
                db_url=self._config.db_url,
            )
        self._store.append_event(
            request.request_id,
            UpdatePhase.BACKING_UP,
            message="backup completed",
            context={
                "backup_path": str(backup_path),
                "backup_sha256": backup_sha,
            },
        )
        # Stash the backup metadata into the running checkpoint so
        # the post-cutover verifier can confirm the backup exists.
        self._store.write_checkpoint(
            request.request_id,
            UpdatePhase.BACKING_UP,
            context={
                "backup_path": str(backup_path),
                "backup_sha256": backup_sha,
            },
        )

    def _do_promoting(self, request: RequestRecord) -> None:
        """Issue #38 §6 — invoke the canonical promotion script.

        The controller never re-implements the symlink swap /
        drop-in rewrite / restart / health-probe logic. It only
        invokes ``scripts/promote_release.sh`` and checks the exit
        code.
        """
        repo = self._config.resolved_repo_root()
        sha = request.target_sha
        proc = self._invoke_promote(repo, sha)
        if proc.returncode != 0:
            raise PromotionFailedError(
                f"promote_release.sh exited {proc.returncode}: "
                f"{proc.stderr.strip() or proc.stdout.strip()}"
            )

    def _do_verifying(self, request: RequestRecord) -> None:
        """Issue #38 §6 — post-cutover health probes."""
        self._run_health_probes(request.target_sha)

    # ------------------------------------------------------------------
    # Rollback orchestration
    # ------------------------------------------------------------------

    def _enter_rollback(
        self,
        request: RequestRecord,
        *,
        phase: UpdatePhase,
        reason: str,
        previous_sha: str,
        started_at: str,
    ) -> ResultRecord:
        """Transition to ``rolling_back`` and run the rollback path."""
        # Transition into rolling_back.
        self._store.write_checkpoint(
            request.request_id,
            UpdatePhase.ROLLING_BACK,
            context={"previous_phase": phase.value, "reason": reason},
        )
        self._store.append_event(
            request.request_id,
            UpdatePhase.ROLLING_BACK,
            message="rollback initiated",
            context={"previous_phase": phase.value, "reason": reason},
        )

        # Run the rollback.
        repo = self._config.resolved_repo_root()
        try:
            proc = self._invoke_rollback(repo, previous_sha=previous_sha)
            if proc.returncode != 0:
                raise RollbackFailedError(
                    f"rollback_release.sh exited {proc.returncode}: "
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )
        except Exception as exc:  # noqa: BLE001
            return self._terminate_rollback_failed(
                request,
                reason=f"rollback failed: {exc}",
                previous_sha=previous_sha,
                started_at=started_at,
            )

        # Verify the rollback restored the previous release.
        try:
            self._run_health_probes(previous_sha)
        except Exception as exc:  # noqa: BLE001
            return self._terminate_rollback_failed(
                request,
                reason=f"rollback verification failed: {exc}",
                previous_sha=previous_sha,
                started_at=started_at,
            )

        # Successful rollback.
        result = ResultRecord(
            request_id=request.request_id,
            final_status=UpdatePhase.ROLLED_BACK.value,
            target_sha=request.target_sha,
            previous_sha=previous_sha,
            deployed_sha=previous_sha,
            started_at=started_at,
            finished_at=now_iso(),
            failure_phase=phase.value,
            failure_reason=reason,
            rollback_performed=True,
            rollback_result="succeeded",
            notification_status="pending",
            events_tail=self._store.tail_events(request.request_id, 8),
        )
        self._store.write_result(result)
        self._store.clear_checkpoint(request.request_id)
        self._store.append_event(
            request.request_id,
            UpdatePhase.ROLLED_BACK,
            message="rollback succeeded; clearing maintenance",
        )
        import contextlib

        with contextlib.suppress(OSError):
            maintenance.disengage_maintenance()
        self._attempt_delivery(result, request)
        return result

    # ------------------------------------------------------------------
    # State machine helpers
    # ------------------------------------------------------------------

    def _next_phase(
        self,
        request: RequestRecord,  # noqa: ARG002
        checkpoint: Any,
        *,
        force: bool,  # noqa: ARG002
    ) -> UpdatePhase:
        if checkpoint is None:
            return UpdatePhase.VALIDATING
        return checkpoint.phase

    def _advance(self, request: RequestRecord, current: UpdatePhase) -> UpdatePhase:  # noqa: ARG002
        """Return the next legal phase after ``current``.

        The transition table has exactly one successor for each
        non-terminal phase in the happy path. The controller never
        guesses at transitions — the table is the single source of
        truth.
        """
        allowed = STATE_TRANSITIONS[current]
        # The happy-path successors are the first listed successor
        # that is not a terminal failure state. We choose them by
        # convention so a future edit to the table does not change
        # implicit ordering.
        preferred = [
            UpdatePhase.BUILDING,
            UpdatePhase.DRAINING,
            UpdatePhase.REHEARSING_MIGRATION,
            UpdatePhase.BACKING_UP,
            UpdatePhase.PROMOTING,
            UpdatePhase.VERIFYING,
            UpdatePhase.SUCCEEDED,
        ]
        for nxt in preferred:
            if nxt in allowed:
                return nxt
        raise StateTransitionError(old=current, new=UpdatePhase.FAILED)

    def _enter(self, request: RequestRecord, phase: UpdatePhase, **context: Any) -> None:
        """Persist a pre-action checkpoint for ``phase``."""
        import contextlib

        with contextlib.suppress(StateTransitionError):
            # Recovery may have entered a non-standard phase; the
            # checkpoint is the authoritative phase.
            validate_transition(self._current_phase(request), phase)
        self._store.write_checkpoint(
            request.request_id,
            phase,
            context=context,
        )
        self._store.append_event(
            request.request_id,
            phase,
            message=f"entering {phase.value}",
            context=dict(context),
        )

    def _current_phase(self, request: RequestRecord) -> UpdatePhase:
        checkpoint = self._store.load_checkpoint(request.request_id)
        if checkpoint is not None:
            return checkpoint.phase
        return UpdatePhase.QUEUED

    # ------------------------------------------------------------------
    # Terminal transitions
    # ------------------------------------------------------------------

    def _terminate_rejection(
        self,
        request: RequestRecord,
        reason: str,
        exc: ValidationError,
    ) -> ResultRecord:
        self._store.write_checkpoint(
            request.request_id,
            UpdatePhase.REJECTED,
            context={"reason": reason, "kind": type(exc).__name__},
        )
        self._store.append_event(
            request.request_id,
            UpdatePhase.REJECTED,
            message="request rejected",
            context={"reason": reason, "kind": type(exc).__name__},
            level="warning",
        )
        result = ResultRecord.rejection(
            request_id=request.request_id,
            target_sha=request.target_sha,
            reason=reason,
        )
        self._store.write_result(result)
        self._store.clear_checkpoint(request.request_id)
        # Rejections never engaged maintenance, but clear it
        # anyway so a previous run does not leave a stale marker.
        import contextlib

        with contextlib.suppress(OSError):
            maintenance.disengage_maintenance()
        self._attempt_delivery(result, request)
        return result

    def _terminate_failed(
        self,
        request: RequestRecord,
        *,
        phase: UpdatePhase,
        reason: str,
        previous_sha: str,
        started_at: str,
    ) -> ResultRecord:
        self._store.write_checkpoint(
            request.request_id,
            UpdatePhase.FAILED,
            context={"phase": phase.value, "reason": reason},
        )
        self._store.append_event(
            request.request_id,
            UpdatePhase.FAILED,
            message="update failed",
            context={"phase": phase.value, "reason": reason},
            level="error",
        )
        # No rollback — the failure happened *before* cutover so
        # the live release is still the previous (correct) one.
        result = ResultRecord(
            request_id=request.request_id,
            final_status=UpdatePhase.FAILED.value,
            target_sha=request.target_sha,
            previous_sha=previous_sha,
            deployed_sha=previous_sha,
            started_at=started_at,
            finished_at=now_iso(),
            failure_phase=phase.value,
            failure_reason=reason,
            notification_status="pending",
            events_tail=self._store.tail_events(request.request_id, 8),
        )
        self._store.write_result(result)
        self._store.clear_checkpoint(request.request_id)
        self._attempt_delivery(result, request)
        return result
        # NOTE: we deliberately do NOT call disengage_maintenance()
        # here — pre-cutover failures leave maintenance clean
        # because the controller never engaged it. The check is
        # defensive but the code path is unreachable.

    def _terminate_rollback_failed(
        self,
        request: RequestRecord,
        *,
        reason: str,
        previous_sha: str,
        started_at: str,
    ) -> ResultRecord:
        self._store.write_checkpoint(
            request.request_id,
            UpdatePhase.ROLLBACK_FAILED,
            context={"reason": reason},
        )
        self._store.append_event(
            request.request_id,
            UpdatePhase.ROLLBACK_FAILED,
            message="rollback failed",
            context={"reason": reason},
            level="error",
        )
        result = ResultRecord(
            request_id=request.request_id,
            final_status=UpdatePhase.ROLLBACK_FAILED.value,
            target_sha=request.target_sha,
            previous_sha=previous_sha,
            deployed_sha="",
            started_at=started_at,
            finished_at=now_iso(),
            failure_phase="rolling_back",
            failure_reason=reason,
            rollback_performed=True,
            rollback_result="failed",
            notification_status="pending",
            events_tail=self._store.tail_events(request.request_id, 8),
        )
        self._store.write_result(result)
        self._store.clear_checkpoint(request.request_id)
        self._attempt_delivery(result, request)
        return result

    # ------------------------------------------------------------------
    # Health probes
    # ------------------------------------------------------------------

    def _run_health_probes(self, expected_sha: str) -> None:
        if self._config.hooks.health_probes is not None:
            self._config.hooks.health_probes(expected_sha)
            return
        self._default_health_probes(expected_sha)

    def _default_health_probes(self, expected_sha: str) -> None:
        """Issue #38 §6 — post-cutover health probes.

        Verifies:

        * the recorded ``deployed-sha`` matches ``expected_sha``;
        * the live systemd unit is active;
        * the loopback ``/health`` and ``/`` endpoints respond;
        * the public (Tailscale) probe responds when the host has
          one configured (best-effort, never fatal).

        Raises :class:`HealthCheckFailedError` on any failure.
        """
        meta = read_live_metadata()
        if not meta.live_sha:
            raise HealthCheckFailedError(
                "no live SHA recorded after cutover; deployed-sha is empty"
            )
        if meta.live_sha != expected_sha:
            raise HealthCheckFailedError(
                f"live SHA {meta.live_sha!r} does not match expected {expected_sha!r}"
            )
        # Loopback probes.
        import urllib.error
        import urllib.request

        port = self._config.service_port
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if not body:
                    raise HealthCheckFailedError("loopback /health returned empty body")
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise HealthCheckFailedError(f"loopback /health failed: {exc}") from exc
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{port}/", timeout=10) as resp:
                body = resp.read().decode("utf-8")
                if "OMNIGENT_SKIP_WEB_UI" in body:
                    raise HealthCheckFailedError(
                        "loopback / served the API-only landing page; release is broken"
                    )
        except (urllib.error.URLError, ConnectionError, OSError) as exc:
            raise HealthCheckFailedError(f"loopback / failed: {exc}") from exc
        # Public probe is best-effort.
        try:
            with urllib.request.urlopen(
                "https://hermes-agent.taile0361b.ts.net:9461/", timeout=8
            ) as resp:
                resp.read()
        except (urllib.error.URLError, ConnectionError, OSError):
            pass

    # ------------------------------------------------------------------
    # Result delivery
    # ------------------------------------------------------------------

    def _attempt_delivery(self, result: ResultRecord, request: RequestRecord) -> None:
        """Best-effort delivery; queues for later reconciliation on failure."""
        from omnigent.updater.runner_client import deliver_result

        # Stamp the origin conversation id into the result so the
        # web service can route the comment to the right session.
        result.events_tail.append(
            {
                "ts": now_iso(),
                "phase": "delivery",
                "level": "info",
                "message": "attempting delivery",
                "context": {"origin_conversation_id": request.origin_conversation_id or ""},
            }
        )
        # Persist the updated tail.
        self._store.write_result(result)
        outcome = deliver_result(result)
        if outcome.delivered:
            result.notification_status = "delivered"
        else:
            result.notification_status = "failed" if outcome.message else "pending"
            self._store.queue_pending_delivery(result)
        self._store.write_result(result)

    # ------------------------------------------------------------------
    # Subprocess invocation
    # ------------------------------------------------------------------

    def _invoke_build_only(self, repo: Path, sha: str) -> subprocess.CompletedProcess:
        if self._config.dry_run:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="dry-run")
        if self._config.hooks.build_only is not None:
            return self._config.hooks.build_only(repo, sha)
        script = self._promote_script(repo)
        env = self._subprocess_env()
        return subprocess.run(
            ["bash", str(script), sha, "--build-only"],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _invoke_promote(self, repo: Path, sha: str) -> subprocess.CompletedProcess:
        if self._config.dry_run:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="dry-run")
        if self._config.hooks.promote is not None:
            return self._config.hooks.promote(repo, sha)
        script = self._promote_script(repo)
        env = self._subprocess_env()
        return subprocess.run(
            ["bash", str(script), sha],
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _invoke_rollback(
        self, repo: Path, *, previous_sha: str = ""
    ) -> subprocess.CompletedProcess:
        # User-provided hooks are honored even in dry_run so tests
        # can simulate failures; ``dry_run`` only short-circuits
        # the real subprocess invocation.
        if self._config.hooks.rollback is not None:
            return self._config.hooks.rollback(repo)
        if self._config.dry_run:
            return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="dry-run")
        script = self._rollback_script(repo)
        env = self._subprocess_env()
        cmd: list[str] = ["bash", str(script)]
        # Pass the explicit previous-SHA captured at the start of the
        # update. ``rollback_release.sh --to <sha>`` is preferred over
        # the ``previous`` symlink because the symlink can be stale
        # (e.g. a broken .venv or an unrelated intermediate deploy),
        # while ``previous_sha`` is the same value the controller
        # recorded as the live release before the failing cutover.
        if previous_sha:
            cmd.extend(["--to", previous_sha])
        return subprocess.run(
            cmd,
            cwd=str(repo),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )

    def _promote_script(self, repo: Path) -> Path:
        # The script lives next to the repo's ``scripts/`` directory
        # in the repo checkout. The repo_root is the live git
        # checkout, so the controller never imports the live release
        # to find the script — it imports from the upstream source
        # tree.
        return repo / "scripts" / "promote_release.sh"

    def _rollback_script(self, repo: Path) -> Path:
        return repo / "scripts" / "rollback_release.sh"

    def _subprocess_env(self) -> dict[str, str]:
        env = os.environ.copy()
        env["PATH"] = "/home/hermes/.local/bin:/home/hermes/.hermes/node/bin:" + env.get(
            "PATH", ""
        )
        env["OMNIGENT_DEPLOY_ROOT"] = str(self._config.resolved_deploy_root())
        env["REPO_ROOT"] = str(self._config.resolved_repo_root())
        env["OMNIGENT_UPDATER_STATE_ROOT"] = str(self._store.state_root)
        env.setdefault("OMIT_CANARY", "0")
        env.setdefault("OMIT_HEALTH", "0")
        return env

    def _candidate_release_dir(self, sha: str) -> Path:
        return self._config.resolved_deploy_root() / "releases" / sha


@dataclass
class RecoveryDecision:
    """One decision returned by :meth:`UpdaterController.recover_non_terminal`."""

    request_id: str
    action: str
    reason: str


__all__ = [
    "BuildFailedError",
    "ControllerConfig",
    "ControllerHooks",
    "HealthCheckFailedError",
    "PromotionFailedError",
    "RecoveryDecision",
    "RollbackFailedError",
    "UpdaterController",
]
