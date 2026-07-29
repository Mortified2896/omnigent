"""Single-active-update locking (issue #38 §1, §9).

The updater enforces the invariant "only one production update may
be active at a time" with two complementary locks:

* a **global** lock — at most one non-terminal update across the
  entire state root;
* a **per-request** lock — exactly one controller instance owns a
  given request id.

Both locks are implemented with the same primitives:

* an ``O_CREAT|O_EXCL`` lock file inside the ``locks/`` directory;
* the holder's pid and start time recorded as JSON inside the lock
  file;
* a stale-lock sweep on acquisition that frees locks older than a
  configurable grace period (default: 1 hour), so a crashed updater
  does not deadlock the system forever.

Both locks are *advisory* — the controller is the only writer, but
the lock files live on disk so an operator inspecting the state root
can see exactly who owns what.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from omnigent.updater import layout

_GLOBAL_LOCK_NAME = "__global__.lock"
DEFAULT_STALE_SECONDS = 3600


@dataclass(frozen=True)
class LockHolder:
    """Identity of a lock holder, persisted inside the lock file."""

    pid: int
    hostname: str
    started_at: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "pid": self.pid,
            "hostname": self.hostname,
            "started_at": self.started_at,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> LockHolder:
        return cls(
            pid=int(data["pid"]),
            hostname=str(data["hostname"]),
            started_at=str(data["started_at"]),
            note=str(data.get("note", "")),
        )


class LockHeldError(RuntimeError):
    """Raised when an ``acquire`` call collides with an active holder."""


def _holder_payload(holder: LockHolder) -> bytes:
    return json.dumps(holder.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"


def _read_holder(path: Path) -> LockHolder | None:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_bytes())
    except (OSError, json.JSONDecodeError):
        return None
    try:
        return LockHolder.from_dict(data)
    except (KeyError, TypeError, ValueError):
        return None


def _pid_alive(pid: int) -> bool:
    """Best-effort "is this pid still alive" check.

    Returns ``False`` for any pid that ``kill -0`` reports as missing
    (ESRCH) or for which we lack permission (EPERM still means the
    pid exists). Returns ``True`` for our own pid so the holder can
    reacquire its own lock after a restart.
    """
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _now_epoch() -> float:
    return time.time()


def _sweep_stale_lock(path: Path, *, stale_seconds: int) -> bool:
    """If ``path`` is held by a dead pid OR is older than
    ``stale_seconds``, remove it and return ``True``."""
    holder = _read_holder(path)
    if holder is None:
        # File is unreadable or malformed — treat as stale.
        try:
            path.unlink()
            return True
        except OSError:
            return False
    if not _pid_alive(holder.pid):
        try:
            path.unlink()
            return True
        except OSError:
            return False
    try:
        started = time.mktime(time.strptime(holder.started_at, "%Y-%m-%dT%H:%M:%SZ"))
    except (ValueError, TypeError):
        started = 0.0
    if stale_seconds > 0 and (_now_epoch() - started) > stale_seconds:
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def _acquire(path: Path, holder: LockHolder, *, stale_seconds: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = _holder_payload(holder)
    fd, tmp_path = None, None
    try:
        import tempfile

        fd, tmp_path = tempfile.mkstemp(
            dir=str(path.parent),
            prefix=f".{path.name}.",
            suffix=".tmp",
        )
        with os.fdopen(fd, "wb") as f:
            f.write(body)
            f.flush()
            os.fsync(f.fileno())
        try:
            os.link(tmp_path, path)
        except FileExistsError as exc:
            # Someone else holds the lock — sweep stale, then retry
            # exactly once. If the second attempt also loses, the
            # lock is genuinely held by a live process.
            if _sweep_stale_lock(path, stale_seconds=stale_seconds):
                try:
                    os.link(tmp_path, path)
                except FileExistsError:
                    raise LockHeldError(f"lock held after stale sweep: {path}") from exc
            else:
                raise LockHeldError(f"lock already held: {path}") from exc
        # Best-effort directory fsync so the lock is durable across crashes.
        try:
            dir_fd = os.open(str(path.parent), os.O_DIRECTORY)
        except OSError:
            dir_fd = -1
        if dir_fd != -1:
            with suppress(OSError):
                os.fsync(dir_fd)
            os.close(dir_fd)
    finally:
        if tmp_path is not None:
            with suppress(OSError):
                os.unlink(tmp_path)


def _release(path: Path, holder: LockHolder) -> None:
    current = _read_holder(path)
    if current is None:
        return
    if current.pid != holder.pid:
        # We don't own the lock anymore; nothing to do.
        return
    with suppress(OSError):
        path.unlink()


def make_holder(*, note: str = "") -> LockHolder:
    """Build a :class:`LockHolder` for the current process."""
    return LockHolder(
        pid=os.getpid(),
        hostname=os.uname().nodename,
        started_at=_now_iso(),
        note=note,
    )


class UpdateLock:
    """A per-request or global advisory lock.

    Use as a context manager::

        with UpdateLock.global_lock() as lock:
            ...

    Or per-request::

        with UpdateLock.for_request(request_id) as lock:
            ...

    The lock is released on context-manager exit. Crashes leave the
    lock on disk until :meth:`sweep` removes it after the grace
    period.
    """

    def __init__(self, *, path: Path, holder: LockHolder, stale_seconds: int) -> None:
        self._path = path
        self._holder = holder
        self._stale_seconds = stale_seconds
        self._held = False

    @classmethod
    def global_lock(
        cls,
        *,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        note: str = "",
    ) -> UpdateLock:
        path = layout.locks_dir() / _GLOBAL_LOCK_NAME
        return cls(path=path, holder=make_holder(note=note), stale_seconds=stale_seconds)

    @classmethod
    def for_request(
        cls,
        request_id: str,
        *,
        stale_seconds: int = DEFAULT_STALE_SECONDS,
        note: str = "",
    ) -> UpdateLock:
        path = layout.lock_path(request_id)
        return cls(path=path, holder=make_holder(note=note), stale_seconds=stale_seconds)

    def acquire(self) -> None:
        if self._held:
            return
        _acquire(self._path, self._holder, stale_seconds=self._stale_seconds)
        self._held = True

    def release(self) -> None:
        if not self._held:
            return
        _release(self._path, self._holder)
        self._held = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def holder(self) -> LockHolder:
        return self._holder

    def __enter__(self) -> UpdateLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.release()

    @staticmethod
    def sweep(stale_seconds: int = DEFAULT_STALE_SECONDS) -> list[Path]:
        """Remove stale locks. Returns the paths that were swept."""
        swept: list[Path] = []
        for entry in layout.locks_dir().iterdir():
            if entry.suffix != ".lock":
                continue
            if _sweep_stale_lock(entry, stale_seconds=stale_seconds):
                swept.append(entry)
        return swept


@contextmanager
def global_lock(
    *, stale_seconds: int = DEFAULT_STALE_SECONDS, note: str = ""
) -> Iterator[UpdateLock]:
    """Sugar around :meth:`UpdateLock.global_lock`."""
    lock = UpdateLock.global_lock(stale_seconds=stale_seconds, note=note)
    lock.acquire()
    try:
        yield lock
    finally:
        lock.release()
