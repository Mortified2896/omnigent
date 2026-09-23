"""Disposable single-service activation executor.

This prototype deliberately has no CLI, systemd adapter, or application
endpoint. It can operate only below a temporary root and delegates process,
write-fence, drain, and runtime verification operations to a fixed adapter.
The browser/application cannot select paths or commands through an activation
request.

The accepted-release byte format is the current ``acceptance-v2.json`` record.
No O1/O2 peer identity is imported here.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID, uuid4

from . import accepted, core

_TERMINAL_STATUSES = {"committed", "rolled_back", "refused", "recovery_required"}
_STATE_DB = "chat.db"
_STATE_DB_SIDECARS = frozenset({"chat.db-wal", "chat.db-shm"})


class ExecutorError(RuntimeError):
    """The disposable executor cannot prove an activation precondition."""


class ActivationBusy(ExecutorError):
    """Another activation or unresolved transaction owns the service."""


class DrainTimeout(ExecutorError):
    """The fenced service did not reach a stable zero-work observation."""


@dataclass(frozen=True)
class DisposableLayout:
    """Fixed filesystem locations below a disposable temporary directory."""

    root: Path
    release_root_override: Path | None = None
    acceptance_root_override: Path | None = None

    def __post_init__(self) -> None:
        resolved = self.root.resolve()
        temporary_root = Path(tempfile.gettempdir()).resolve()
        try:
            resolved.relative_to(temporary_root)
        except ValueError as exc:
            raise ExecutorError("disposable layout must stay under the temporary root") from exc
        if resolved == temporary_root:
            raise ExecutorError("disposable layout needs its own child directory")
        for external_path in (self.release_root_override, self.acceptance_root_override):
            if external_path is None:
                continue
            try:
                external_path.resolve().relative_to(temporary_root)
            except ValueError as exc:
                raise ExecutorError(
                    "disposable release/evidence roots must stay under tmp"
                ) from exc

    @property
    def service_root(self) -> Path:
        return self.root / "service"

    @property
    def release_root(self) -> Path:
        return self.release_root_override or self.root / "releases"

    @property
    def acceptance_root(self) -> Path:
        return self.acceptance_root_override or self.root / "artifacts"

    @property
    def state_root(self) -> Path:
        return self.service_root / "state"

    @property
    def current(self) -> Path:
        return self.service_root / "current"

    @property
    def previous(self) -> Path:
        return self.service_root / "previous"

    @property
    def transactions(self) -> Path:
        return self.service_root / "transactions"

    def initialize(self) -> None:
        """Create only the fixed directories owned by this temporary rig."""

        for path in (
            self.root,
            self.service_root,
            self.state_root,
            self.transactions,
        ):
            if path.is_symlink():
                raise ExecutorError(f"disposable directory is a symlink: {path}")
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
            if not path.is_dir():
                raise ExecutorError(f"disposable path is not a directory: {path}")
        for path in (self.release_root, self.acceptance_root):
            if path.is_symlink() or not path.is_dir():
                raise ExecutorError(
                    f"disposable release/evidence root is missing or indirect: {path}"
                )


@dataclass(frozen=True)
class AcceptedRelease:
    """One release bound to an existing immutable acceptance record."""

    identity: core.ReleaseIdentity
    root: Path
    acceptance_path: Path
    record: dict[str, Any]


class AcceptedReleaseCatalog:
    """Resolve exact SHAs and verify their current accepted bytes."""

    def __init__(
        self,
        layout: DisposableLayout,
        *,
        owner_uid: int = 0,
        source_artifacts_root: Path | None = None,
        source_release_root: Path | None = None,
        source_owner_uid: int = 0,
        uv_executable: str | None = "/srv/tools/uv-0.12.1/bin/uv",
        run_uv_check: bool = True,
    ) -> None:
        if (source_artifacts_root is None) != (source_release_root is None):
            raise ExecutorError("mirrored artifacts need both source roots")
        self.layout = layout
        self.owner_uid = owner_uid
        self.source_artifacts_root = source_artifacts_root
        self.source_release_root = source_release_root
        self.source_owner_uid = source_owner_uid
        self.uv_executable = uv_executable
        self.run_uv_check = run_uv_check

    def load(
        self,
        source_sha: str,
        *,
        expected_digest: str | None = None,
    ) -> AcceptedRelease:
        if len(source_sha) != 40 or any(ch not in "0123456789abcdef" for ch in source_sha):
            raise ExecutorError("release SHA must be a lowercase 40-character commit")
        release_root = self.layout.release_root / source_sha
        acceptance_path = self.layout.acceptance_root / source_sha / "acceptance-v2.json"
        if release_root.is_symlink() or not release_root.is_dir():
            raise ExecutorError(f"accepted release is missing or indirect: {release_root}")
        if acceptance_path.is_symlink() or not acceptance_path.is_file():
            raise ExecutorError(
                f"accepted release evidence is missing or indirect: {acceptance_path}"
            )
        try:
            record = accepted.load_acceptance_record(
                acceptance_path,
                artifacts_root=self.layout.acceptance_root,
                expected_digest=expected_digest,
                owner_uid=self.owner_uid,
            )
        except accepted.AcceptedArtifactError as exc:
            raise ExecutorError(f"accepted-release evidence rejected: {exc}") from exc
        if record.get("source_sha") != source_sha:
            raise ExecutorError("acceptance source SHA differs from the selected SHA")
        acceptance_digest = accepted.canonical_digest(record)
        bound_release_root = self.source_release_root
        if self.source_artifacts_root is not None:
            assert self.source_release_root is not None
            source_path = self.source_artifacts_root / source_sha / "acceptance-v2.json"
            try:
                source_record = accepted.load_acceptance_record(
                    source_path,
                    artifacts_root=self.source_artifacts_root,
                    expected_digest=acceptance_digest,
                    owner_uid=self.source_owner_uid,
                )
            except accepted.AcceptedArtifactError as exc:
                raise ExecutorError(f"source acceptance record rejected: {exc}") from exc
            if source_record != record:
                raise ExecutorError("temporary acceptance mirror differs from its pinned source")
        elif Path(str(record.get("runtime", ""))) != release_root:
            raise ExecutorError("acceptance record names a different immutable release path")
        try:
            record = accepted.verify_accepted_artifact(
                acceptance_path,
                acceptance_digest,
                artifacts_root=self.layout.acceptance_root,
                release_root=self.layout.release_root,
                owner_uid=self.owner_uid,
                bound_release_root=bound_release_root,
                uv_executable=self.uv_executable,
                run_uv_check=self.run_uv_check,
            )
        except accepted.AcceptedArtifactError as exc:
            raise ExecutorError(f"accepted-release bytes failed verification: {exc}") from exc
        info = record["info"]
        return AcceptedRelease(
            identity=core.ReleaseIdentity(
                source_sha=source_sha,
                acceptance_digest=acceptance_digest,
                package_version=record.get("package_version") or info.get("server_version"),
                schema_revision=record["schema"],
            ),
            root=release_root,
            acceptance_path=acceptance_path,
            record=record,
        )


class RuntimeAdapter(Protocol):
    """Fixed host-owned operations required by the generic executor."""

    def readiness(self) -> core.ControllerReadiness:
        """Report externally proven fence, drain, restart, and independence."""

    def observe(self) -> core.RuntimeObservation:
        """Return the running process and its state generation, failing closed."""

    def writes_are_fenced(self) -> bool:
        """Return the observed ingress fence state, not a requested value."""

    def fence_writes(self) -> None:
        """Stop admitting writes while leaving read access available."""

    def open_writes(self) -> None:
        """Reopen writes after activation or verified rollback."""

    def is_running(self) -> bool:
        """Report whether this service process is running."""

    def stop(self) -> None:
        """Stop only the one disposable service managed by this adapter."""

    def start(self, release: AcceptedRelease) -> None:
        """Start the exact supplied immutable release."""

    def verify(self, release: AcceptedRelease) -> None:
        """Check runtime health, build/acceptance identity, schema, UI, and API."""


class DisposableExecutor:
    """Coordinate activation and rollback in one disposable service layout."""

    def __init__(
        self,
        layout: DisposableLayout,
        adapter: RuntimeAdapter,
        *,
        owner_uid: int = 0,
        source_artifacts_root: Path | None = None,
        source_release_root: Path | None = None,
        source_owner_uid: int = 0,
        uv_executable: str | None = "/srv/tools/uv-0.12.1/bin/uv",
        run_uv_check: bool = True,
        now: Callable[[], float] = time.time,
        sleep: Callable[[float], None] = time.sleep,
        drain_timeout: float = 30.0,
        poll_interval: float = 0.25,
        stable_idle_samples: int = 2,
    ) -> None:
        if drain_timeout <= 0 or poll_interval < 0 or stable_idle_samples < 2:
            raise ValueError("invalid drain policy")
        self.layout = layout
        self.adapter = adapter
        self.catalog = AcceptedReleaseCatalog(
            layout,
            owner_uid=owner_uid,
            source_artifacts_root=source_artifacts_root,
            source_release_root=source_release_root,
            source_owner_uid=source_owner_uid,
            uv_executable=uv_executable,
            run_uv_check=run_uv_check,
        )
        self.now = now
        self.sleep = sleep
        self.drain_timeout = drain_timeout
        self.poll_interval = poll_interval
        self.stable_idle_samples = stable_idle_samples
        self.layout.initialize()

    def plan(self, candidate_sha: str) -> core.ActivationPlan:
        """Resolve accepted identities and prepare a short-lived exact plan."""

        now = self.now()
        current_pointer = _read_pointer(self.layout.current, self.layout.release_root)
        current_release = self.catalog.load(current_pointer.name)
        observed = self.adapter.observe()
        if observed.release.source_sha != current_release.identity.source_sha:
            raise ExecutorError("running build SHA differs from the current release pointer")
        if (
            observed.release.acceptance_digest is not None
            and observed.release.acceptance_digest != current_release.identity.acceptance_digest
        ):
            raise ExecutorError("running acceptance identity differs from the current pointer")
        current = replace(
            observed,
            release=current_release.identity,
            observed_at=now,
        )
        candidate_release = self.catalog.load(candidate_sha)
        previous_release = None
        if self.layout.previous.exists() or self.layout.previous.is_symlink():
            previous_pointer = _read_pointer(self.layout.previous, self.layout.release_root)
            previous_release = self.catalog.load(previous_pointer.name)

        record = candidate_release.record
        checks = record["checks"]
        candidate = core.CandidateEvidence(
            release=candidate_release.identity,
            observed_at=now,
            bytes_verified=True,
            isolated_boot_ok=checks.get("isolated_boot") is True,
            health_ok=checks.get("isolated_boot") is True,
            build_identity_ok=checks.get("build_identity") is True,
            frontend_ok=checks.get("frontend") is True,
            rollback_from_current_ok=(
                candidate_release.identity.schema_revision
                == current_release.identity.schema_revision
            ),
        )
        readiness = self.adapter.readiness()
        readiness = replace(readiness, observed_at=now, serialized=True)
        if not _state_backup_ready(self.layout.state_root):
            readiness = replace(readiness, backup_ready=False)
        if not current_release.identity.complete():
            readiness = replace(readiness, rollback_ready=False)
        readiness = replace(
            readiness,
            previous_release_retained=current_release.root.is_dir(),
        )
        return core.plan_activation(
            current,
            candidate,
            previous_release.identity if previous_release else None,
            readiness,
            now=now,
        )

    def activate(
        self,
        request: Mapping[str, Any],
        *,
        failpoint: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Execute one fresh compare-and-swap request under the service lock."""

        key = _request_key(request)
        candidate_sha = _request_candidate_sha(request)
        request_digest = _json_digest(dict(request))
        transaction_dir = self.layout.transactions / key
        tx_path = transaction_dir / "transaction.json"
        with self._locked():
            if tx_path.exists() or tx_path.is_symlink():
                prior = _read_json(tx_path)
                if prior.get("request_digest") != request_digest:
                    raise ExecutorError("idempotency key was reused for a different request")
                if prior.get("status") in _TERMINAL_STATUSES:
                    return prior
                raise ActivationBusy("activation request already has an unresolved transaction")
            if transaction_dir.exists() or transaction_dir.is_symlink():
                raise ActivationBusy("transaction directory exists without a valid journal")
            self._require_no_unresolved_transaction()
            fresh_plan = self.plan(candidate_sha)
            core.validate_activation_request(request, fresh_plan, now=self.now())
            current_release = self.catalog.load(
                fresh_plan.current.release.source_sha or "",
                expected_digest=fresh_plan.current.release.acceptance_digest,
            )
            candidate_release = self.catalog.load(
                candidate_sha,
                expected_digest=fresh_plan.candidate.release.acceptance_digest,
            )
            previous_before = _pointer_target(self.layout.previous, self.layout.release_root)
            current_before = _read_pointer(self.layout.current, self.layout.release_root)
            if current_before != current_release.root:
                raise ExecutorError("current release pointer changed after evidence collection")
            self.adapter.verify(current_release)
            transaction_dir.mkdir(mode=0o700)
            _fsync_dir(self.layout.transactions)
            record: dict[str, Any] = {
                "schema_version": 1,
                "idempotency_key": key,
                "request_digest": request_digest,
                "expected": fresh_plan.expected(),
                "current_release": _release_document(current_release),
                "candidate_release": _release_document(candidate_release),
                "previous_before": str(previous_before) if previous_before else None,
                "current_before": str(current_before),
                "phase": core.ActivationPhase.PLANNED.value,
                "status": "in_progress",
                "writes_reopen_started": False,
                "writes_reopened": False,
                "backup": None,
                "error": None,
            }
            # The durable request and transaction journal share one canonical
            # record; its transaction directory also owns the backup evidence.
            self._save(tx_path, record, exclusive=True)
            self._trip(failpoint, "journaled", record)
            try:
                self._advance(record, core.ActivationPhase.FENCING, tx_path, transaction_dir)
                self.adapter.fence_writes()
                if not self.adapter.writes_are_fenced():
                    raise ExecutorError("write fence was not confirmed")
                record["writes_fenced"] = True
                self._save_both(record, tx_path, transaction_dir)
                self._trip(failpoint, "fenced", record)

                self._advance(record, core.ActivationPhase.DRAINING, tx_path, transaction_dir)
                quiesced = self._wait_for_zero_work(current_release, record)
                record["quiesced_state_generation"] = quiesced.state_generation
                self._advance(record, core.ActivationPhase.QUIESCED, tx_path, transaction_dir)
                self._trip(failpoint, "quiesced", record)

                # The process is stopped only after every admitted turn has
                # drained. A stopped process gives the SQLite snapshot a clear
                # boundary and prevents new local writes during the backup.
                self.adapter.stop()
                if self.adapter.is_running():
                    raise ExecutorError("service remained running after stop")
                record["service_stopped"] = True
                self._save_both(record, tx_path, transaction_dir)
                backup = _create_state_backup(
                    self.layout.state_root,
                    transaction_dir,
                    expected_schema=current_release.identity.schema_revision or "",
                )
                record["backup"] = backup
                record["backup_verified"] = True
                self._advance(record, core.ActivationPhase.BACKED_UP, tx_path, transaction_dir)
                self._trip(failpoint, "backed_up", record)

                # Repeat byte verification immediately before pointer mutation.
                candidate_release = self.catalog.load(
                    candidate_sha,
                    expected_digest=candidate_release.identity.acceptance_digest,
                )
                _set_pointer(
                    self.layout.previous,
                    current_release.root,
                    self.layout.release_root,
                    key,
                )
                record["previous_switched"] = True
                self._save_both(record, tx_path, transaction_dir)
                self._trip(failpoint, "previous_switched", record)
                _set_pointer(
                    self.layout.current, candidate_release.root, self.layout.release_root, key
                )
                record["current_switched"] = True
                self._advance(record, core.ActivationPhase.SWITCHED, tx_path, transaction_dir)
                self._trip(failpoint, "current_switched", record)

                self._advance(record, core.ActivationPhase.STARTING, tx_path, transaction_dir)
                self.adapter.start(candidate_release)
                record["service_stopped"] = False
                self._advance(record, core.ActivationPhase.VERIFYING, tx_path, transaction_dir)
                self.adapter.verify(candidate_release)
                self._trip(failpoint, "verified", record)

                # Commit the exact accepted candidate while still fenced. The
                # next durable phase is an uncertainty barrier: an interruption
                # inside open_writes() forbids restoring the stale-state backup.
                record["status"] = "committing"
                self._advance(record, core.ActivationPhase.COMMITTED, tx_path, transaction_dir)
                self._trip(failpoint, "committed_fenced", record)
                record["writes_reopen_started"] = True
                self._advance(
                    record, core.ActivationPhase.REOPENING_WRITES, tx_path, transaction_dir
                )
                self.adapter.open_writes()
                if self.adapter.writes_are_fenced():
                    raise ExecutorError("write fence remained closed after reopen")
                record["writes_reopened"] = True
                record["status"] = "committed"
                self._advance(
                    record, core.ActivationPhase.WRITES_REOPENED, tx_path, transaction_dir
                )
                self._trip(failpoint, "writes_reopened", record)
                return record
            except Exception as exc:  # noqa: BLE001 - recover from adapter/process failures.
                record["error"] = f"{type(exc).__name__}: {exc}"
                self._save_both(record, tx_path, transaction_dir)
                if _writes_may_be_open(record, self.adapter):
                    self._best_effort_fence(record, tx_path, transaction_dir)
                    self._mark_recovery_required(
                        record,
                        tx_path,
                        transaction_dir,
                        "writes reopened or reopening is uncertain; "
                        "persistent state was preserved",
                    )
                elif (
                    _pointer_target(self.layout.current, self.layout.release_root)
                    == current_release.root
                ):
                    self._abort_before_switch(record, tx_path, transaction_dir, current_release)
                else:
                    self._rollback(record, tx_path, transaction_dir, current_release)
                return record

    def recover(self, idempotency_key: str) -> dict[str, Any]:
        """Resume an interrupted transaction without crossing the state fence."""

        key = _validate_key(idempotency_key)
        transaction_dir = self.layout.transactions / key
        tx_path = transaction_dir / "transaction.json"
        with self._locked(recovering=key):
            record = _read_json(tx_path)
            if record.get("idempotency_key") != key:
                raise ExecutorError("transaction identity mismatch")
            if record.get("status") in _TERMINAL_STATUSES:
                return record
            current_doc = record.get("current_release")
            if not isinstance(current_doc, dict):
                raise ExecutorError("journal has no pinned current release")
            current = self.catalog.load(
                str(current_doc.get("source_sha", "")),
                expected_digest=str(current_doc.get("acceptance_digest", "")),
            )
            if _writes_may_be_open(record, self.adapter):
                self._best_effort_fence(record, tx_path, transaction_dir)
                self._mark_recovery_required(
                    record,
                    tx_path,
                    transaction_dir,
                    "write reopening may have completed; automatic state restore is forbidden",
                )
                return record
            active = _pointer_target(self.layout.current, self.layout.release_root)
            if active not in {current.root, Path(str(record["candidate_release"]["root"]))}:
                self._mark_recovery_required(
                    record,
                    tx_path,
                    transaction_dir,
                    "current pointer is outside the transaction's exact release pair",
                )
                return record
            if active == current.root:
                try:
                    # Reconcile the pointer even when its post-switch journal
                    # write was interrupted. The old current pointer proves
                    # candidate activation did not begin.
                    self._restore_previous_pointer(record, key)
                except Exception as exc:  # noqa: BLE001 - persist recovery evidence.
                    self._mark_recovery_required(
                        record,
                        tx_path,
                        transaction_dir,
                        f"pre-switch recovery failed: {type(exc).__name__}: {exc}",
                    )
                    return record
                return self._restart_and_reopen_old(record, tx_path, transaction_dir, current)
            self.catalog.load(
                str(record["candidate_release"]["source_sha"]),
                expected_digest=str(record["candidate_release"]["acceptance_digest"]),
            )
            if record.get("backup_verified") is not True:
                self._mark_recovery_required(
                    record,
                    tx_path,
                    transaction_dir,
                    "candidate pointer changed before a verified persistent-state backup existed",
                )
                return record
            self._rollback(record, tx_path, transaction_dir, current)
            return record

    @contextmanager
    def _locked(self, *, recovering: str | None = None) -> Iterator[None]:
        self.layout.transactions.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.layout.transactions / "activation.lock"
        if lock_path.is_symlink():
            raise ExecutorError("activation lock path is a symlink")
        stream = lock_path.open("a+")
        try:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            stream.close()
            raise ActivationBusy("another activation owns the service lock") from exc
        try:
            self._require_no_unresolved_transaction_except(recovering)
            yield
        finally:
            fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            stream.close()

    def _require_no_unresolved_transaction(self) -> None:
        self._require_no_unresolved_transaction_except(None)

    def _require_no_unresolved_transaction_except(self, recovering: str | None) -> None:
        for tx_dir in self.layout.transactions.iterdir():
            if tx_dir.name == "activation.lock":
                continue
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise ExecutorError(f"unexpected entry in transaction root: {tx_dir}")
            if recovering is not None and tx_dir.name == recovering:
                continue
            path = tx_dir / "transaction.json"
            if not path.exists():
                raise ActivationBusy(f"transaction directory has no journal: {tx_dir.name}")
            record = _read_json(path)
            if record.get("status") not in _TERMINAL_STATUSES:
                raise ActivationBusy(f"unresolved activation requires recovery: {tx_dir.name}")

    def _save(self, path: Path, record: Mapping[str, Any], *, exclusive: bool = False) -> None:
        _durable_json(path, dict(record), exclusive=exclusive)

    def _save_both(self, record: dict[str, Any], tx_path: Path, tx_dir: Path) -> None:
        del tx_dir
        _durable_json(tx_path, record)

    def _advance(
        self,
        record: dict[str, Any],
        phase: core.ActivationPhase,
        tx_path: Path,
        tx_dir: Path,
    ) -> None:
        current_phase = core.ActivationPhase(record["phase"])
        if not core.transition_allowed(current_phase, phase):
            raise ExecutorError(f"illegal activation phase transition: {current_phase} -> {phase}")
        record["phase"] = phase.value
        record["updated_at"] = self.now()
        self._save_both(record, tx_path, tx_dir)

    def _trip(
        self,
        failpoint: Callable[[str, Mapping[str, Any]], None] | None,
        name: str,
        record: Mapping[str, Any],
    ) -> None:
        if failpoint is not None:
            failpoint(name, dict(record))

    def _wait_for_zero_work(
        self,
        current: AcceptedRelease,
        record: Mapping[str, Any],
    ) -> core.RuntimeObservation:
        deadline = time.monotonic() + self.drain_timeout
        idle_generation: str | None = None
        idle_samples = 0
        while True:
            observed = self.adapter.observe()
            if not self.adapter.writes_are_fenced() or observed.writes_fenced is not True:
                raise ExecutorError("write fence was lost during drain")
            if (
                observed.release.source_sha != current.identity.source_sha
                or observed.process_generation
                != record["expected"]["current"]["process_generation"]
                or observed.state_identity != record["expected"]["current"]["state_identity"]
            ):
                raise ExecutorError("process, release, or state identity changed during drain")
            if type(observed.active_work) is not int or observed.active_work < 0:
                raise ExecutorError("active-work count is unknown after write fence")
            if not observed.state_generation:
                raise ExecutorError("state generation is unknown after write fence")
            if observed.active_work:
                idle_samples = 0
                idle_generation = None
            elif observed.state_generation == idle_generation:
                idle_samples += 1
            else:
                idle_generation = observed.state_generation
                idle_samples = 1
            if idle_samples >= self.stable_idle_samples:
                return observed
            if time.monotonic() >= deadline:
                raise DrainTimeout("active work did not reach stable zero before the deadline")
            self.sleep(self.poll_interval)

    def _abort_before_switch(
        self,
        record: dict[str, Any],
        tx_path: Path,
        tx_dir: Path,
        current: AcceptedRelease,
    ) -> None:
        try:
            active = _pointer_target(self.layout.current, self.layout.release_root)
            if active != current.root:
                raise ExecutorError("current release changed during pre-switch abort")
            running = self.adapter.is_running()
            if running and record.get("service_stopped"):
                raise ExecutorError("journal says stopped but the service is running")
            if record.get("backup_verified") is True:
                if running:
                    raise ExecutorError("service must be stopped before state restore")
                _verify_state_backup(record["backup"], tx_dir)
                _restore_state_backup(
                    self.layout.state_root,
                    record["backup"],
                    tx_dir,
                    record,
                    lambda: self._save_both(record, tx_path, tx_dir),
                )
            if not running:
                self.adapter.start(current)
            self.adapter.verify(current)
            if self.adapter.writes_are_fenced():
                self.adapter.open_writes()
            if self.adapter.writes_are_fenced():
                raise ExecutorError("write fence could not be removed after pre-switch abort")
            if record.get("service_stopped") or record.get("backup_verified"):
                self._advance(record, core.ActivationPhase.ROLLING_BACK, tx_path, tx_dir)
                self._advance(record, core.ActivationPhase.ROLLED_BACK, tx_path, tx_dir)
                record["status"] = "rolled_back"
            else:
                self._advance(record, core.ActivationPhase.REFUSED, tx_path, tx_dir)
                record["status"] = "refused"
            record["updated_at"] = self.now()
            self._save_both(record, tx_path, tx_dir)
        except Exception as exc:  # noqa: BLE001 - preserve a failed rollback for recovery.
            self._mark_recovery_required(
                record,
                tx_path,
                tx_dir,
                "pre-switch abort could not verify/reopen the current service: "
                f"{type(exc).__name__}: {exc}",
            )

    def _rollback(
        self,
        record: dict[str, Any],
        tx_path: Path,
        tx_dir: Path,
        current: AcceptedRelease,
    ) -> None:
        phase = core.ActivationPhase(record["phase"])
        may_be_open = _writes_may_be_open(record, self.adapter)
        if not core.automatic_rollback_allowed(
            phase=phase,
            writes_reopened=may_be_open,
            backup_verified=record.get("backup_verified") is True,
            previous_release_verified=current.identity.complete(),
        ):
            self._best_effort_fence(record, tx_path, tx_dir)
            self._mark_recovery_required(
                record,
                tx_path,
                tx_dir,
                "automatic rollback is not safe with current write-fence/backup evidence",
            )
            return
        try:
            if not self.adapter.writes_are_fenced():
                raise ExecutorError("write fence is not confirmed during rollback")
            self._advance(record, core.ActivationPhase.ROLLING_BACK, tx_path, tx_dir)
            if self.adapter.is_running():
                self.adapter.stop()
            _verify_state_backup(record["backup"], tx_dir)
            _restore_state_backup(
                self.layout.state_root,
                record["backup"],
                tx_dir,
                record,
                lambda: self._save_both(record, tx_path, tx_dir),
            )
            self._restore_previous_pointer(record, str(record["idempotency_key"]))
            _set_pointer(self.layout.current, current.root, self.layout.release_root, "rollback")
            verified_current = self.catalog.load(
                current.identity.source_sha or "",
                expected_digest=current.identity.acceptance_digest,
            )
            self.adapter.start(verified_current)
            self.adapter.verify(verified_current)
            if not self.adapter.writes_are_fenced():
                raise ExecutorError("write fence disappeared before rollback verification")
            self.adapter.open_writes()
            if self.adapter.writes_are_fenced():
                raise ExecutorError("write fence remained closed after verified rollback")
            record["rollback_verified"] = True
            record["writes_reopened"] = True
            record["status"] = "rolled_back"
            self._advance(record, core.ActivationPhase.ROLLED_BACK, tx_path, tx_dir)
            self._save_both(record, tx_path, tx_dir)
        except Exception as exc:  # noqa: BLE001 - preserve a failed rollback for recovery.
            self._best_effort_fence(record, tx_path, tx_dir)
            self._mark_recovery_required(
                record,
                tx_path,
                tx_dir,
                f"verified rollback failed: {type(exc).__name__}: {exc}",
            )

    def _restore_previous_pointer(self, record: Mapping[str, Any], key: str) -> None:
        previous_before = record.get("previous_before")
        if previous_before is None:
            _remove_pointer(self.layout.previous, self.layout.release_root)
        else:
            previous = Path(str(previous_before))
            _check_release_target(previous, self.layout.release_root)
            _set_pointer(self.layout.previous, previous, self.layout.release_root, key + "-prev")

    def _restart_and_reopen_old(
        self,
        record: dict[str, Any],
        tx_path: Path,
        tx_dir: Path,
        current: AcceptedRelease,
    ) -> dict[str, Any]:
        try:
            if not self.adapter.writes_are_fenced():
                raise ExecutorError("write fence is not confirmed during interrupted recovery")
            if self.adapter.is_running():
                self.adapter.stop()
            if record.get("previous_switched") is True:
                self._restore_previous_pointer(record, str(record["idempotency_key"]))
            if record.get("backup_verified") is True:
                _verify_state_backup(record["backup"], tx_dir)
                # The current pointer never moved; no candidate writes were
                # admitted. Restore the post-drain backup only after verifying it.
                _restore_state_backup(
                    self.layout.state_root,
                    record["backup"],
                    tx_dir,
                    record,
                    lambda: self._save_both(record, tx_path, tx_dir),
                )
                self._advance(record, core.ActivationPhase.ROLLING_BACK, tx_path, tx_dir)
            if not self.adapter.is_running():
                self.adapter.start(current)
            self.adapter.verify(current)
            self.adapter.open_writes()
            if self.adapter.writes_are_fenced():
                raise ExecutorError("write fence remained closed after recovery")
            record["rollback_verified"] = True
            record["writes_reopened"] = True
            if record.get("backup_verified") is True:
                record["status"] = "rolled_back"
                self._advance(record, core.ActivationPhase.ROLLED_BACK, tx_path, tx_dir)
            else:
                record["status"] = "refused"
                self._advance(record, core.ActivationPhase.REFUSED, tx_path, tx_dir)
            self._save_both(record, tx_path, tx_dir)
        except Exception as exc:  # noqa: BLE001 - persist recovery evidence.
            self._best_effort_fence(record, tx_path, tx_dir)
            self._mark_recovery_required(
                record,
                tx_path,
                tx_dir,
                f"interrupted pre-switch recovery failed: {type(exc).__name__}: {exc}",
            )
        return record

    def _best_effort_fence(self, record: dict[str, Any], tx_path: Path, tx_dir: Path) -> None:
        try:
            self.adapter.fence_writes()
            record["writes_fenced"] = self.adapter.writes_are_fenced()
            self._save_both(record, tx_path, tx_dir)
        except Exception as exc:  # noqa: BLE001 - fencing is best effort after failure.
            record["fence_error"] = f"{type(exc).__name__}: {exc}"
            self._save_both(record, tx_path, tx_dir)

    def _mark_recovery_required(
        self,
        record: dict[str, Any],
        tx_path: Path,
        tx_dir: Path,
        reason: str,
    ) -> None:
        record["recovery_reason"] = reason
        record["status"] = "recovery_required"
        phase = core.ActivationPhase(record["phase"])
        if phase is not core.ActivationPhase.RECOVERY_REQUIRED and core.transition_allowed(
            phase, core.ActivationPhase.RECOVERY_REQUIRED
        ):
            record["phase"] = core.ActivationPhase.RECOVERY_REQUIRED.value
        record["updated_at"] = self.now()
        self._save_both(record, tx_path, tx_dir)


def _request_key(request: Mapping[str, Any]) -> str:
    return _validate_key(request.get("idempotency_key"))


def _validate_key(value: Any) -> str:
    try:
        parsed = UUID(value) if isinstance(value, str) else None
    except ValueError:
        parsed = None
    if parsed is None or parsed.version != 4 or str(parsed) != value:
        raise core.ActivationRefused("invalid idempotency key")
    return str(parsed)


def _request_candidate_sha(request: Mapping[str, Any]) -> str:
    expected = request.get("expected")
    candidate = expected.get("candidate") if isinstance(expected, Mapping) else None
    sha = candidate.get("source_sha") if isinstance(candidate, Mapping) else None
    if not isinstance(sha, str):
        raise core.ActivationRefused("request has no pinned candidate SHA")
    return sha


def _release_document(release: AcceptedRelease) -> dict[str, Any]:
    return {
        **release.identity.pins(),
        "root": str(release.root),
        "acceptance_path": str(release.acceptance_path),
    }


def _read_pointer(path: Path, release_root: Path) -> Path:
    if not path.is_symlink():
        raise ExecutorError(f"release pointer is missing or not a symlink: {path}")
    target = path.resolve(strict=True)
    _check_release_target(target, release_root)
    return target


def _pointer_target(path: Path, release_root: Path) -> Path | None:
    if not path.exists() and not path.is_symlink():
        return None
    return _read_pointer(path, release_root)


def _check_release_target(target: Path, release_root: Path) -> None:
    root = release_root.resolve()
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise ExecutorError(f"release pointer escapes immutable release root: {target}") from exc
    if target.parent != root or re.fullmatch(r"[a-f0-9]{40}", target.name) is None:
        raise ExecutorError(f"release pointer must name one exact SHA directory: {target}")
    if target.is_symlink() or not target.is_dir():
        raise ExecutorError(f"release pointer target is missing or indirect: {target}")


def _set_pointer(path: Path, target: Path, release_root: Path, nonce: str) -> None:
    _check_release_target(target.resolve(strict=True), release_root)
    if path.exists() and not path.is_symlink():
        raise ExecutorError(f"refusing to replace a non-symlink release pointer: {path}")
    temporary = path.parent / f".{path.name}-{nonce}.tmp"
    if temporary.exists() or temporary.is_symlink():
        raise ExecutorError(f"release pointer staging path already exists: {temporary}")
    temporary.symlink_to(target.resolve(strict=True))
    os.replace(temporary, path)
    _fsync_dir(path.parent)


def _remove_pointer(path: Path, release_root: Path) -> None:
    if not path.exists() and not path.is_symlink():
        return
    _read_pointer(path, release_root)
    path.unlink()
    _fsync_dir(path.parent)


def _durable_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    data = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temporary = path.with_name(f".{path.name}.{uuid4()}.tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    _fsync_dir(path.parent)


def _read_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise ExecutorError(f"transaction journal is missing or indirect: {path}")
    try:
        value = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ExecutorError(f"transaction journal cannot be read: {path}") from exc
    if not isinstance(value, dict):
        raise ExecutorError(f"transaction journal is not a JSON object: {path}")
    return value


def _json_digest(value: dict[str, Any]) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(payload).hexdigest()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _fsync_tree(root: Path) -> None:
    directories = [root]
    for path in root.rglob("*"):
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutorError(f"refusing to sync an indirect state file: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            directories.append(path)
        elif stat.S_ISREG(metadata.st_mode):
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        else:
            raise ExecutorError(f"unsupported persistent-state entry: {path}")
    for directory in reversed(directories):
        _fsync_dir(directory)


def _state_backup_ready(state_root: Path) -> bool:
    database = state_root / _STATE_DB
    return (
        state_root.is_dir()
        and not state_root.is_symlink()
        and database.is_file()
        and not database.is_symlink()
        and os.access(state_root, os.W_OK)
        and os.access(database, os.R_OK)
    )


def _state_schema(database: Path) -> str:
    if database.is_symlink() or not database.is_file():
        raise ExecutorError(f"persistent database is missing or indirect: {database}")
    try:
        with sqlite3.connect(f"file:{database}?mode=ro", uri=True) as connection:
            if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ExecutorError("persistent database integrity check failed")
            rows = connection.execute("SELECT version_num FROM alembic_version").fetchall()
    except sqlite3.Error as exc:
        raise ExecutorError(f"persistent database schema cannot be read: {exc}") from exc
    if len(rows) != 1 or not isinstance(rows[0][0], str):
        raise ExecutorError("persistent database must have one Alembic schema head")
    return rows[0][0]


def _tree_digest(root: Path, *, exclude_top_names: frozenset[str] = frozenset()) -> str:
    entries: list[dict[str, Any]] = []
    if root.is_symlink() or not root.is_dir():
        raise ExecutorError(f"state copy is missing or indirect: {root}")
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        if relative.split("/", 1)[0] in exclude_top_names:
            continue
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ExecutorError(f"persistent state contains a symlink: {path}")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "kind": "dir", "mode": metadata.st_mode & 0o777})
        elif stat.S_ISREG(metadata.st_mode):
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": metadata.st_mode & 0o777,
                    "sha256": accepted.file_digest(path),
                }
            )
        else:
            raise ExecutorError(f"unsupported persistent-state entry: {path}")
    return hashlib.sha256(
        json.dumps({"entries": entries}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _reject_state_symlinks(root: Path) -> None:
    for path in (root, *root.rglob("*")):
        if path.is_symlink():
            raise ExecutorError(f"persistent state contains an indirect path: {path}")


def _create_state_backup(
    state_root: Path,
    transaction_dir: Path,
    *,
    expected_schema: str,
) -> dict[str, Any]:
    if state_root.is_symlink() or not state_root.is_dir():
        raise ExecutorError("persistent state root is missing or indirect")
    _reject_state_symlinks(state_root)
    database = state_root / _STATE_DB
    live_schema = _state_schema(database)
    if live_schema != expected_schema:
        raise ExecutorError("live database schema differs from the accepted release")
    backup_root = transaction_dir / "state-backup"
    if backup_root.exists() or backup_root.is_symlink():
        raise ExecutorError("persistent-state backup already exists")
    backup_root.mkdir(mode=0o700)
    extra = backup_root / "extra"
    extra.mkdir(mode=0o700)
    for child in state_root.iterdir():
        if child.name in {_STATE_DB, f"{_STATE_DB}-wal", f"{_STATE_DB}-shm"}:
            continue
        destination = extra / child.name
        if child.is_dir():
            shutil.copytree(child, destination, symlinks=False)
        else:
            shutil.copy2(child, destination, follow_symlinks=False)
    backup_db = backup_root / _STATE_DB
    try:
        with (
            sqlite3.connect(f"file:{database}?mode=ro", uri=True) as source,
            sqlite3.connect(backup_db) as target,
        ):
            source.backup(target)
            if target.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                raise ExecutorError("persistent-state backup integrity check failed")
    except sqlite3.Error as exc:
        raise ExecutorError(f"persistent-state backup failed: {exc}") from exc
    if _state_schema(backup_db) != expected_schema:
        raise ExecutorError("persistent-state backup schema differs from the accepted release")
    _fsync_tree(backup_root)
    _fsync_dir(transaction_dir)
    return {
        "root": str(backup_root),
        "database": str(backup_db),
        "database_sha256": accepted.file_digest(backup_db),
        "extra": str(extra),
        "extra_tree_sha256": _tree_digest(extra),
        "schema": expected_schema,
    }


def _verify_state_backup(backup: Mapping[str, Any], transaction_dir: Path) -> None:
    expected_root = transaction_dir / "state-backup"
    root = Path(str(backup.get("root", "")))
    database = Path(str(backup.get("database", "")))
    extra = Path(str(backup.get("extra", "")))
    if root != expected_root or database != root / _STATE_DB or extra != root / "extra":
        raise ExecutorError("persistent-state backup path is outside its transaction")
    if root.is_symlink() or database.is_symlink() or extra.is_symlink():
        raise ExecutorError("persistent-state backup contains an indirect path")
    if accepted.file_digest(database) != backup.get("database_sha256"):
        raise ExecutorError("persistent-state backup database digest changed")
    if _tree_digest(extra) != backup.get("extra_tree_sha256"):
        raise ExecutorError("persistent-state backup files digest changed")
    if _state_schema(database) != backup.get("schema"):
        raise ExecutorError("persistent-state backup schema changed")


def _restore_state_backup(
    state_root: Path,
    backup: Mapping[str, Any],
    transaction_dir: Path,
    record: dict[str, Any],
    save: Callable[[], None],
) -> None:
    _verify_state_backup(backup, transaction_dir)
    restore_phase = record.get("state_restore_phase")
    stage_value = record.get("state_restore_stage")
    stage = Path(str(stage_value)) if stage_value else transaction_dir / f"restore-stage-{uuid4()}"
    failed = transaction_dir / "state-before-rollback"
    if not stage_value:
        record["state_restore_stage"] = str(stage)
        record["state_restore_phase"] = "preparing"
        save()
    if restore_phase in (None, "preparing"):
        if stage.exists() or stage.is_symlink():
            if (
                stage.is_symlink()
                or accepted.file_digest(stage / _STATE_DB) != backup.get("database_sha256")
                or _tree_digest(
                    stage,
                    exclude_top_names=frozenset({_STATE_DB, *_STATE_DB_SIDECARS}),
                )
                != backup.get("extra_tree_sha256")
            ):
                raise ExecutorError(
                    "partial restore staging failed verification; evidence preserved"
                )
        else:
            shutil.copytree(Path(str(backup["extra"])), stage, symlinks=False)
            shutil.copy2(Path(str(backup["database"])), stage / _STATE_DB)
            _fsync_tree(stage)
        if accepted.file_digest(stage / _STATE_DB) != backup.get("database_sha256"):
            raise ExecutorError("staged persistent-state database digest mismatch")
        if _state_schema(stage / _STATE_DB) != backup.get("schema"):
            raise ExecutorError("staged persistent-state schema mismatch")
        if _tree_digest(
            stage, exclude_top_names=frozenset({_STATE_DB, *_STATE_DB_SIDECARS})
        ) != backup.get("extra_tree_sha256"):
            raise ExecutorError("staged persistent-state files digest mismatch")
        record["state_restore_phase"] = "staged"
        save()
    if state_root.exists() or state_root.is_symlink():
        if state_root.is_symlink():
            raise ExecutorError("persistent-state root became a symlink during restore")
        if not failed.exists():
            os.replace(state_root, failed)
            _fsync_dir(state_root.parent)
            _fsync_dir(transaction_dir)
            record["state_restore_phase"] = "old_state_moved"
            save()
    if not state_root.exists():
        if not stage.is_dir() or stage.is_symlink():
            raise ExecutorError("verified restore staging is missing")
        os.replace(stage, state_root)
        _fsync_dir(state_root.parent)
        _fsync_dir(transaction_dir)
    if _state_schema(state_root / _STATE_DB) != backup.get("schema"):
        raise ExecutorError("restored persistent-state schema mismatch")
    if accepted.file_digest(state_root / _STATE_DB) != backup.get("database_sha256"):
        raise ExecutorError("restored persistent-state database digest mismatch")
    record["state_restore_phase"] = "restored"
    save()


def _writes_may_be_open(record: Mapping[str, Any], adapter: RuntimeAdapter) -> bool:
    if (
        record.get("writes_reopen_started") is True
        or record.get("writes_reopened") is True
        or record.get("phase")
        in {
            core.ActivationPhase.REOPENING_WRITES.value,
            core.ActivationPhase.WRITES_REOPENED.value,
        }
    ):
        return True
    try:
        writes_are_fenced = adapter.writes_are_fenced()
    except Exception:  # noqa: BLE001 - an unknown fence state forbids stale-state restore.
        return True
    return record.get("writes_fenced") is True and not writes_are_fenced


__all__ = [
    "AcceptedRelease",
    "AcceptedReleaseCatalog",
    "ActivationBusy",
    "DisposableExecutor",
    "DisposableLayout",
    "DrainTimeout",
    "ExecutorError",
    "RuntimeAdapter",
]
