"""Verified service-state helper for the peer-supervised deployer.

systemctl ``is-active`` returns a non-zero exit status when the service is
not active. Under ``set -o pipefail`` the broken pattern

    systemctl is-active NAME | grep -q '^inactive$'

fails whenever the service is *correctly* reporting ``inactive`` because
``systemctl`` exits with status 3. This module exposes a vetted helper that
distinguishes active, inactive, failed, activating, and unknown states
without relying on exit codes that mean "not active".

Used by every Control Room deployment script and exercised by the
regression tests in ``tests/deploy/test_peer_deployer_service_state.py``.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterable
from typing import Final

# These are the *only* outputs that count as "active" for promotion
# acceptance. ``reloading`` and ``activating`` are treated as not-yet-
# active: a freshly-restarted service that is still answering health
# probes is acceptable, but the deployment toolkit must distinguish
# "still starting" from "running".
ACTIVE_STATES: Final = frozenset({"active"})
OK_STATES: Final = frozenset({"active", "reloading", "activating"})
INACTIVE_STATES: Final = frozenset({"inactive", "deactivating"})
FAILED_STATES: Final = frozenset({"failed"})
ERROR_STATES: Final = frozenset({"failed", "inactive", "auto-restart", "maintenance"})


class ServiceStateError(RuntimeError):
    """Raised when a service-state probe cannot be performed."""


def _coerce_state(raw: str) -> str:
    state = raw.strip().lower()
    if not state:
        raise ServiceStateError("systemctl is-active returned empty output")
    return state


def is_active(unit: str) -> bool:
    """Return ``True`` iff the unit is in the ``active`` state.

    Implemented by capturing ``systemctl is-active`` output to stdout
    rather than relying on its exit code. The exit code is intentionally
    ignored — the *only* accepted definition of "active" is the token
    ``active`` on stdout. A unit not known to systemd is NOT active.
    """
    if not unit:
        raise ServiceStateError("unit name is required")
    if not is_known(unit):
        return False
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    try:
        state = _coerce_state(result.stdout)
    except ServiceStateError:
        # Genuine nothing — surface the underlying systemctl error.
        stderr = (result.stderr or "").strip()
        raise ServiceStateError(
            f"systemctl is-active {unit!r} returned no state: {stderr or 'unknown error'}"
        ) from None
    return state in ACTIVE_STATES


def get_state(unit: str) -> str:
    """Return the raw ``systemctl is-active`` output, lowercased and stripped.

    This is the canonical helper for callers that want to make an
    explicit decision based on the state token. It raises
    ``ServiceStateError`` if the unit is not recognized by systemd,
    using ``systemctl cat`` as the authoritative check. The
    ``systemctl is-active`` output alone is unreliable on units that
    were never installed — on some systems it returns ``inactive``
    even for unknown units.
    """
    if not unit:
        raise ServiceStateError("unit name is required")
    if not is_known(unit):
        stderr = "unit not known to systemd"
        raise ServiceStateError(
            f"systemctl is-active {unit!r} target unknown: {stderr}"
        )
    result = subprocess.run(
        ["systemctl", "is-active", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    if not result.stdout.strip():
        stderr = (result.stderr or "").strip()
        raise ServiceStateError(
            f"systemctl is-active {unit!r} returned no state: {stderr or 'unknown error'}"
        )
    return _coerce_state(result.stdout)


def is_inactive(unit: str) -> bool:
    """Return ``True`` iff the unit is in the ``inactive`` state.

    Does NOT use the broken ``is-active | grep -q '^inactive$'`` pattern.
    A unit that is not known to systemd is treated as ``inactive``
    (fail-safe: a post-restart check should not silently succeed on
    an unknown unit).
    """
    if not is_known(unit):
        return True
    try:
        return get_state(unit) in INACTIVE_STATES
    except ServiceStateError:
        return False


def is_failed(unit: str) -> bool:
    """Return ``True`` iff the unit is in the ``failed`` state."""
    if not is_known(unit):
        return False
    try:
        return get_state(unit) in FAILED_STATES
    except ServiceStateError:
        return False


def is_known(unit: str) -> bool:
    """Return ``True`` iff systemd recognizes the unit."""
    if not unit:
        return False
    result = subprocess.run(
        ["systemctl", "cat", unit],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def wait_for_state(
    unit: str,
    *,
    desired: str,
    timeout_s: float,
    poll_interval_s: float = 0.5,
) -> bool:
    """Poll ``systemctl is-active`` until ``desired`` is observed.

    Resolves to ``True`` on success, ``False`` on timeout. ``desired``
    must be one of the canonical state tokens. This is the helper that
    replaces the broken ``is-active | grep`` retry-loop pattern.
    """
    if desired not in {"active", "inactive", "failed"}:
        raise ServiceStateError(f"unsupported desired state: {desired!r}")
    if not unit:
        raise ServiceStateError("unit name is required")
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            state = get_state(unit)
        except ServiceStateError:
            state = "unknown"
        if state == desired:
            return True
        time.sleep(poll_interval_s)
    return False


def classify(*, active: Iterable[str] = (), inactive: Iterable[str] = (),
             failed: Iterable[str] = (), unknown: Iterable[str] = ()) -> dict:
    """Return a deterministic state report for a peer-supervised deployment.

    Each iterable is a list of unit names. The returned dict has the
    same keys plus ``summary``. The helper is intended for the
    preflight gate of the peer-deployer.
    """
    report: dict = {
        "active": list(active),
        "inactive": list(inactive),
        "failed": list(failed),
        "unknown": list(unknown),
        "summary": "ok",
    }
    if report["failed"]:
        report["summary"] = "failed"
    elif report["unknown"]:
        report["summary"] = "unknown"
    elif report["inactive"]:
        report["summary"] = "inactive"
    return report


__all__ = [
    "ServiceStateError",
    "ACTIVE_STATES",
    "OK_STATES",
    "INACTIVE_STATES",
    "FAILED_STATES",
    "ERROR_STATES",
    "is_active",
    "is_inactive",
    "is_failed",
    "is_known",
    "get_state",
    "wait_for_state",
    "classify",
]
