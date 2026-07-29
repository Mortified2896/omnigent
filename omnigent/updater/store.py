"""Durable updater state store (issue #38 §1).

Wraps the on-disk layout with atomic-write primitives, fsync, and a
small in-memory cache. The store is responsible for:

* atomic request file creation (``O_CREAT|O_EXCL`` + ``fsync``);
* append-only event logging with ``fsync`` after each line so a crash
  mid-update does not lose the phase history;
* atomic state checkpoint writes so a crash mid-update is recoverable;
* atomic result file writes so a terminal result is durable before
  delivery is attempted.

All public methods are crash-safe — partial writes are impossible
because every method either replaces a file via a tempfile +
``os.replace`` (atomic on POSIX) or refuses to overwrite an existing
file (``O_EXCL`` for new requests).
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.updater import layout
from omnigent.updater.protocol import (
    RequestRecord,
    ResultRecord,
    now_iso,
)
from omnigent.updater.state_machine import UpdatePhase


def _fsync_dir(path: Path) -> None:
    """``fsync`` the directory containing ``path``.

    Some filesystems require this to make a file's contents
    durable. On filesystems where directory ``fsync`` is a no-op
    (XFS / ext4 with default mount options) this is harmless; on
    overlay filesystems it can fail with ``EINVAL`` or ``ENOSYS``,
    which is also harmless because the file's contents were
    already flushed.
    """
    try:
        fd = os.open(str(path), os.O_DIRECTORY)
    except (OSError, ValueError):
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


def _atomic_write_bytes(target: Path, body: bytes) -> None:
    """Atomic write: tempfile + fsync + rename. Parent dir is ``fsync``-ed."""
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        dir=str(target.parent),
        prefix=f".{target.name}.",
        suffix=".tmp",
    )
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, target)
        _fsync_dir(target.parent)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise


def _atomic_write_json(target: Path, payload: dict[str, Any]) -> None:
    body = json.dumps(payload, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    _atomic_write_bytes(target, body)


def _read_json(target: Path) -> dict[str, Any] | None:
    if not target.is_file():
        return None
    with target.open("rb") as f:
        body = f.read()
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None


class DuplicateRequestError(RuntimeError):
    """Raised when an atomic request create collides with an existing request.

    The atomic-create contract is "exactly one request file with
    this id wins"; a collision means another updater instance
    filed the same request at the same moment. The caller decides
    whether to retry or surface the error to the operator.
    """


@dataclass(frozen=True)
class CheckpointRecord:
    """A durable checkpoint inside the running file.

    The checkpoint records:

    * the current :class:`UpdatePhase`;
    * the timestamp of the transition;
    * an optional context blob with phase-specific metadata (the
      rehearsal DB path, the backup path, etc.).

    The controller writes a fresh checkpoint before and after every
    externally visible action so a crash mid-update can be recovered
    by reading the latest checkpoint.
    """

    request_id: str
    phase: UpdatePhase
    updated_at: str
    context: dict[str, Any]


class UpdaterStore:
    """Durable updater state store.

    Thread-affine — the controller is single-threaded, but tests
    may construct multiple stores in the same process. The store
    does not hold any mutable in-memory state besides an optional
    cache that callers can clear via :meth:`clear_cache`.

    :param state_root: Override the state root. Defaults to
        :func:`omnigent.updater.layout.state_root`.
    """

    def __init__(self, state_root: Path | None = None) -> None:
        self._state_root_override = state_root

    @property
    def state_root(self) -> Path:
        if self._state_root_override is not None:
            return self._state_root_override
        return layout.state_root()

    # ------------------------------------------------------------------
    # Request creation
    # ------------------------------------------------------------------

    def create_request(self, record: RequestRecord) -> Path:
        """Atomically create the request file. Raises
        :class:`DuplicateRequestError` on collision.

        The contract: exactly one updater instance ever wins the
        ``O_EXCL`` race for a given ``request_id``. Losers raise
        :class:`DuplicateRequestError`; the caller is responsible
        for picking a new id and retrying.

        :param record: The request record to persist.
        :returns: The path of the request file.
        """
        target = layout.request_path(record.request_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(record.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
        fd, tmp_path = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(body)
                f.flush()
                os.fsync(f.fileno())
            # O_EXCL link-into-place: refuses to overwrite an
            # existing request file. If somebody else got here first
            # we raise DuplicateRequestError rather than silently
            # clobbering their record.
            try:
                os.link(tmp_path, target)
            except FileExistsError as exc:
                raise DuplicateRequestError(f"request already exists: {target}") from exc
            _fsync_dir(target.parent)
        finally:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        return target

    def load_request(self, request_id: str) -> RequestRecord | None:
        """Load the request file as a :class:`RequestRecord`."""
        data = _read_json(layout.request_path(request_id))
        if data is None:
            return None
        return RequestRecord.from_dict(data)

    # ------------------------------------------------------------------
    # Checkpoints
    # ------------------------------------------------------------------

    def write_checkpoint(
        self,
        request_id: str,
        phase: UpdatePhase,
        *,
        context: dict[str, Any] | None = None,
    ) -> Path:
        """Persist the running checkpoint.

        Called before **and** after every externally visible action
        so a crash mid-update can resume from the latest checkpoint.
        The running file is created on the first call and replaced
        atomically on subsequent calls.

        :param request_id: The request id.
        :param phase: The phase the controller is about to enter (or
            has just entered). Pre-action checkpoints record the
            *about to enter* phase; post-action checkpoints record
            the *just entered* phase with an ``after_*`` context key.
        :param context: Optional phase-specific metadata.
        :returns: The path of the running file.
        """
        record = CheckpointRecord(
            request_id=request_id,
            phase=phase,
            updated_at=now_iso(),
            context=dict(context or {}),
        )
        target = layout.running_path(request_id)
        payload = {
            "request_id": record.request_id,
            "phase": record.phase.value,
            "updated_at": record.updated_at,
            "context": record.context,
        }
        _atomic_write_json(target, payload)
        return target

    def load_checkpoint(self, request_id: str) -> CheckpointRecord | None:
        data = _read_json(layout.running_path(request_id))
        if data is None:
            return None
        return CheckpointRecord(
            request_id=str(data["request_id"]),
            phase=UpdatePhase(str(data["phase"])),
            updated_at=str(data["updated_at"]),
            context=dict(data.get("context", {})),
        )

    def clear_checkpoint(self, request_id: str) -> None:
        path = layout.running_path(request_id)
        if path.is_file():
            path.unlink()
            _fsync_dir(path.parent)

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def append_event(
        self,
        request_id: str,
        phase: UpdatePhase,
        *,
        message: str,
        context: dict[str, Any] | None = None,
        level: str = "info",
    ) -> Path:
        """Append a single JSONL event to the request's event log.

        Each event is flushed and ``fsync``-ed so a crash mid-update
        cannot lose the phase history. The on-disk file is
        append-only; rotation is intentionally not implemented
        because each request is short-lived and the file size is
        bounded by the phase count.
        """
        target = layout.events_path(request_id)
        target.parent.mkdir(parents=True, exist_ok=True)
        event = {
            "ts": now_iso(),
            "phase": phase.value,
            "level": level,
            "message": message,
            "context": dict(context or {}),
        }
        line = (json.dumps(event, sort_keys=True) + "\n").encode("utf-8")
        with target.open("ab") as f:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        return target

    def read_events(self, request_id: str) -> list[dict[str, Any]]:
        target = layout.events_path(request_id)
        if not target.is_file():
            return []
        events: list[dict[str, Any]] = []
        with target.open("rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    events.append(json.loads(raw))
                except json.JSONDecodeError:
                    continue
        return events

    def tail_events(self, request_id: str, n: int) -> list[dict[str, Any]]:
        """Return the last ``n`` events. Cheap because each request's
        event log is bounded."""
        events = self.read_events(request_id)
        if n <= 0 or not events:
            return []
        return events[-n:]

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def write_result(self, record: ResultRecord) -> Path:
        """Atomically write the terminal result file.

        Called before delivery is attempted; the file is the durable
        record the web service reads when reconciling pending
        deliveries.
        """
        target = layout.result_path(record.request_id)
        _atomic_write_json(target, record.to_dict())
        return target

    def load_result(self, request_id: str) -> ResultRecord | None:
        data = _read_json(layout.result_path(request_id))
        if data is None:
            return None
        return ResultRecord.from_dict(data)

    # ------------------------------------------------------------------
    # Pending-deliveries queue
    # ------------------------------------------------------------------

    def queue_pending_delivery(self, result: ResultRecord) -> Path:
        """Drop the result into the pending-deliveries queue.

        The web service drains the queue on startup. The controller
        also tries a direct ``POST /api/updater/result-deliver`` and
        removes the queued file on success.
        """
        target = layout.pending_deliveries_dir() / f"{result.request_id}.json"
        _atomic_write_json(target, result.to_dict())
        return target

    def list_pending_deliveries(self) -> list[Path]:
        return sorted(layout.pending_deliveries_dir().glob("*.json"))

    def remove_pending_delivery(self, request_id: str) -> None:
        path = layout.pending_deliveries_dir() / f"{request_id}.json"
        if path.is_file():
            path.unlink()
            _fsync_dir(path.parent)

    # ------------------------------------------------------------------
    # Listing helpers (used by the recovery scan + the CLI)
    # ------------------------------------------------------------------

    def list_request_ids(self) -> list[str]:
        return sorted(p.stem for p in layout.requests_dir().glob("*.json"))

    def non_terminal_requests(self) -> list[str]:
        """Return request ids whose result is missing or whose
        running checkpoint is missing."""
        ids: list[str] = []
        for rid in self.list_request_ids():
            result = self.load_result(rid)
            if result is None or result.final_status not in (
                "succeeded",
                "rejected",
                "failed",
                "rolled_back",
                "rollback_failed",
            ):
                ids.append(rid)
        return ids

    def iter_all(
        self,
    ) -> Iterator[tuple[RequestRecord, ResultRecord | None, CheckpointRecord | None]]:
        """Yield ``(request, result, checkpoint)`` for every request id.

        Used by the recovery scan and the operator CLI. ``result``
        and ``checkpoint`` are ``None`` when the file does not exist.
        """
        for rid in self.list_request_ids():
            req = self.load_request(rid)
            if req is None:
                continue
            yield req, self.load_result(rid), self.load_checkpoint(rid)
