"""Maintenance mode and active-session drain (issue #38 §4).

When an update enters ``draining``:

* the controller flips a durable maintenance-mode marker file the
  web service reads on every startup;
* the controller polls the web service's drain endpoint to discover
  which sessions are still active;
* new session / runner creation paths reject requests with a clear
  ``MAINTENANCE_MODE`` response while the marker is set;
* active sessions are **not** killed — they are allowed to finish
  or be aborted explicitly by the operator;
* the controller clears or reconciles the marker on success,
  rejection, failure, rollback, and on its own restart.

The marker file lives outside the deploy root so a release rollback
cannot silently clear it. The web service clears the marker on
startup unless the updater is still running.

The maintenance module is intentionally not coupled to FastAPI or
the existing chat.db schema — it is a small, focused abstraction
the web service can mount without depending on the updater's
internal state machine.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path

from omnigent.updater import layout
from omnigent.updater.protocol import now_iso

DEFAULT_DRAIN_POLL_SECONDS = 2.0
DEFAULT_DRAIN_TIMEOUT_SECONDS = 1800  # 30 minutes


@dataclass(frozen=True)
class MaintenanceState:
    """The durable maintenance-mode state.

    :param active: Whether maintenance mode is currently engaged.
    :param request_id: The update request that engaged maintenance.
    :param set_at: ISO timestamp when the marker was set.
    :param reason: Free-form human reason (audit only).
    """

    active: bool
    request_id: str
    set_at: str
    reason: str = ""

    def to_dict(self) -> dict[str, dict[str, object] | bool | str]:
        return {
            "active": self.active,
            "request_id": self.request_id,
            "set_at": self.set_at,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> MaintenanceState:
        return cls(
            active=bool(data.get("active", False)),
            request_id=str(data.get("request_id", "")),
            set_at=str(data.get("set_at", "")),
            reason=str(data.get("reason", "")),
        )


def read_marker(path: Path | None = None) -> MaintenanceState:
    """Read the current maintenance marker, defaulting to inactive if absent."""
    target = path or layout.maintenance_marker_path()
    if not target.is_file():
        return MaintenanceState(active=False, request_id="", set_at="")
    try:
        data = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError):
        return MaintenanceState(active=False, request_id="", set_at="")
    return MaintenanceState.from_dict(data)


def write_marker(state: MaintenanceState, *, path: Path | None = None) -> Path:
    """Atomic write of the maintenance marker.

    The marker is durable, so this method uses the same atomic
    tempfile + ``fsync`` + rename pattern the request store uses.
    """
    target = path or layout.maintenance_marker_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(state.to_dict(), indent=2, sort_keys=True).encode("utf-8") + b"\n"
    import tempfile

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
        try:
            dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
        except OSError:
            dir_fd = -1
        if dir_fd != -1:
            with contextlib.suppress(OSError):
                os.fsync(dir_fd)
            os.close(dir_fd)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return target


def clear_marker(*, path: Path | None = None) -> None:
    """Remove the marker file.

    Called on success, rejection, rollback, and on the web service's
    startup reconciliation. Idempotent — removing a missing file is
    a no-op.
    """
    target = path or layout.maintenance_marker_path()
    if target.is_file():
        target.unlink()
        try:
            dir_fd = os.open(str(target.parent), os.O_DIRECTORY)
        except OSError:
            dir_fd = -1
        if dir_fd != -1:
            with contextlib.suppress(OSError):
                os.fsync(dir_fd)
            os.close(dir_fd)


@dataclass
class DrainStatus:
    """Drain status reported by the web service.

    :param draining: Whether the web service is in drain mode.
    :param active_sessions: List of session ids still active.
    :param active_runners: List of runner ids still active.
    """

    draining: bool
    active_sessions: list[str] = field(default_factory=list)
    active_runners: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, object]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> DrainStatus:
        return cls(
            draining=bool(data.get("draining", False)),
            active_sessions=list(data.get("active_sessions", []) or []),
            active_runners=list(data.get("active_runners", []) or []),
        )

    @property
    def is_idle(self) -> bool:
        """True iff drain mode is engaged and no sessions / runners remain."""
        return self.draining and not self.active_sessions and not self.active_runners


class DrainPoller:
    """Poll the web service's drain endpoint until it reports idle.

    Used by the controller after engaging maintenance. Active
    sessions are **not** killed — the poller just waits for them
    to finish. The operator can call :meth:`request_cancel` to ask
    the web service to abort active sessions, but the controller
    never does that automatically.
    """

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int | None = None,
        poll_seconds: float = DEFAULT_DRAIN_POLL_SECONDS,
        timeout_seconds: float = DEFAULT_DRAIN_TIMEOUT_SECONDS,
        session: str | None = None,
    ) -> None:
        if port is None:
            port = layout.notify_port()
        self._host = host
        self._port = port
        self._poll_seconds = poll_seconds
        self._timeout_seconds = timeout_seconds
        self._session = session

    def _url(self, path: str) -> str:
        return f"http://{self._host}:{self._port}{path}"

    def fetch(self) -> DrainStatus:
        """Query the web service's drain endpoint.

        Returns a :class:`DrainStatus` reflecting the response.
        Treats network errors as ``draining=False`` (the server is
        unreachable) so the controller can fail loudly rather than
        proceeding into cutover while the web service is offline.
        """
        url = self._url("/api/updater/drain-status")
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, ConnectionError, OSError, json.JSONDecodeError):
            return DrainStatus(draining=False)
        return DrainStatus.from_dict(data)

    def wait_until_idle(
        self,
        *,
        on_poll: callable[[DrainStatus], None] | None = None,
    ) -> DrainStatus:
        """Poll the drain endpoint until idle or timeout.

        :param on_poll: Optional callback invoked after every poll
            so the caller can log progress.
        :returns: The final :class:`DrainStatus`.
        :raises TimeoutError: if the drain does not complete before
            the timeout expires.
        """
        deadline = time.monotonic() + self._timeout_seconds
        last = DrainStatus(draining=False)
        while True:
            last = self.fetch()
            if on_poll is not None:
                with contextlib.suppress(Exception):
                    on_poll(last)
            if last.is_idle:
                return last
            if time.monotonic() > deadline:
                raise TimeoutError(
                    f"drain did not complete within {self._timeout_seconds}s; "
                    f"active_sessions={last.active_sessions}, "
                    f"active_runners={last.active_runners}"
                )
            time.sleep(self._poll_seconds)

    def request_cancel(self, *, session_id: str | None = None) -> None:
        """Best-effort cancel request.

        The web service may refuse if the session is in a state
        where cancelling is unsafe; the controller never assumes
        cancellation succeeded.
        """
        url = self._url("/api/updater/request-cancel")
        body = json.dumps({"session_id": session_id or self._session or ""}).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                resp.read()
        except (urllib.error.URLError, ConnectionError, OSError):
            return


def engage_maintenance(*, request_id: str, reason: str = "") -> MaintenanceState:
    """Engage maintenance mode for the given update request.

    Sets the durable marker file the web service reads. The web
    service also reads the marker on every startup so a crash
    between engage and the next web-service request does not lose
    the maintenance state.
    """
    state = MaintenanceState(
        active=True,
        request_id=request_id,
        set_at=now_iso(),
        reason=reason,
    )
    write_marker(state)
    return state


def disengage_maintenance() -> None:
    """Clear the maintenance marker. Idempotent."""
    clear_marker()


def reconcile_marker(*, owner_pid: int | None) -> MaintenanceState:
    """Reconcile the maintenance marker at startup.

    The web service calls this on every startup. If the marker is
    active but the owning updater process is dead, the marker is
    cleared (the operator is the only one who can re-engage it).
    If the owner is alive, the marker is left in place.

    :param owner_pid: The pid of the controller that set the
        marker, if known. ``None`` means the marker is older than
        the current owner-tracking generation; the safest behavior
        is to leave it alone so the operator can decide.
    """
    state = read_marker()
    if not state.active:
        return state
    if owner_pid is None:
        return state
    try:
        os.kill(owner_pid, 0)
    except ProcessLookupError:
        clear_marker()
        return MaintenanceState(active=False, request_id="", set_at="")
    except PermissionError:
        # Pid exists but we can't signal it; be safe and leave it alone.
        return state
    except OSError:
        return state
    return state


__all__ = [
    "DEFAULT_DRAIN_POLL_SECONDS",
    "DEFAULT_DRAIN_TIMEOUT_SECONDS",
    "DrainPoller",
    "DrainStatus",
    "MaintenanceState",
    "clear_marker",
    "disengage_maintenance",
    "engage_maintenance",
    "read_marker",
    "reconcile_marker",
    "write_marker",
]
