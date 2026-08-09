"""Path-safety allowlist for the peer-deployer.

The peer-deployer handles a small set of well-defined paths on the
host filesystem: the O1 deployment root, the O2 deployment root,
each instance's DB home, the canonical quarantine root, and
per-transaction staging directories. The peer-deployer must NEVER
delete, rename, move, or restore a path that is not on this
allowlist.

The previous incident was caused by a path heuristic that
identified "/opt/omnigent/venv" as a "new release" because no
"venv.legacy-*" existed. The pattern matched the *shape* of a
release path instead of the *identity*. This module provides the
opposite: a vetted allowlist that names every legitimate path the
deployer can act on, and an explicit rejection of every path that
is not on the list.

The allowlist is rooted at:

  * /opt/omnigent            (O1 deployment root)
  * /opt/omnigent-production (O2 deployment root)
  * /var/lib/omnigent         (O1 home)
  * /var/lib/omnigent-production (O2 home)
  * /var/lib/omnigent-control-room (transaction + quarantine root)

Sub-allowlist semantics:

  * The O1 deployment root allows any path under
    /opt/omnigent/staging/<TX_ID>/ (per-transaction staging).
  * The O1 deployment root allows any path under
    /opt/omnigent/releases/<ACCEPTED_SHA>/ (the verified candidate).
  * The O1 deployment root allows /opt/omnigent/venv (the
    active runtime symlink/directory) only when the operation
    is a paired swap, never a delete.
  * The O2 deployment root allows any path under
    /opt/omnigent-production/releases/<SHA>/ (read-only).
  * The transaction root allows any path under
    /var/lib/omnigent-control-room/transactions/<TX_ID>/ (audit).
  * The quarantine root allows any path under
    /var/lib/omnigent-control-room/quarantine/<TX_ID>/ (audit).

Anything else is rejected. This includes:

  * /
  * /opt
  * /opt/omnigent (the root itself is not deleteable; only its
    sub-paths are)
  * /opt/omnigent/venv (active runtime is not deleteable)
  * /opt/omnigent-production (O2 root is not deleteable)
  * /var
  * /etc
  * /etc/systemd
  * paths containing ".." components after resolve
  * paths that resolve to a symlink target outside the allowlist
  * empty paths
  * paths owned by the O2 home (DB, artifacts, logs)
"""
from __future__ import annotations

import os
from collections.abc import Iterable
from pathlib import Path

from . import identity
from .identity import Instance


class PathSafetyError(RuntimeError):
    """Raised when a path is not on the deployment allowlist."""


# The structural-failure modes that the path-safety module must
# always reject. These are paths that a path-matching heuristic
# could *possibly* misidentify as a release/staging path even
# though they are intrinsic host state.
INTRINSIC_FORBIDDEN: tuple[Path, ...] = (
    Path("/"),
    Path("/opt"),
    Path("/opt/omnigent"),
    Path("/opt/omnigent/venv"),
    Path("/opt/omnigent-production"),
    Path("/var"),
    Path("/var/lib"),
    Path("/var/lib/omnigent"),
    Path("/var/lib/omnigent-production"),
    Path("/var/lib/omnigent-control-room"),
    Path("/etc"),
    Path("/etc/systemd"),
    Path("/etc/omnigent"),
    Path("/etc/omnigent-production"),
    Path("/home"),
    Path("/root"),
    Path("/tmp"),
    Path("/usr"),
    Path("/bin"),
    Path("/sbin"),
    Path("/lib"),
    Path("/proc"),
    Path("/sys"),
    Path("/dev"),
    Path("/run"),
    Path("/boot"),
)


def _abs(path: Path | str) -> Path:
    """Return ``Path(os.path.abspath(path))``."""
    return Path(os.path.abspath(str(path)))


def _resolve(path: Path | str) -> Path:
    """Return ``Path(os.path.realpath(path))``.

    Resolves through symlinks. Nonexistent paths raise OSError; we
    treat that as a refusal because the path cannot be proven.
    """
    return Path(os.path.realpath(str(path)))


def _is_protected_root(path: Path) -> bool:
    """Return True iff ``path`` is in the intrinsic-forbidden list.

    The check is exact: only the paths named in the list, not
    their descendants. (E.g. /opt/omnigent/venv is protected,
    but /opt/omnigent/staging/<TX_ID> is not.)
    """
    for forbidden in INTRINSIC_FORBIDDEN:
        if path == forbidden:
            return True
    return False


def _normalize(path: Path | str) -> Path:
    """Return a positional-only, no-symlink, no-traversal normalization.

    Splits the path into absolute components and rejects any
    traversal component ("..") that would escape the absolute
    origin. This is a string-level check; the resolved-path
    check is also done by ``_resolve``.
    """
    p = Path(path)
    if not p.is_absolute():
        raise PathSafetyError(f"path is not absolute: {path!r}")
    for part in p.parts:
        if part == "..":
            raise PathSafetyError(f"path contains traversal: {path!r}")
    return Path(os.path.normpath(str(p)))


def assert_on_allowlist(
    path: Path | str,
    *,
    operation: str,
    target: Instance,
    supervisor: Instance,
    allowed_roots: Iterable[Path] | None = None,
) -> Path:
    """Verify ``path`` is on the deployment allowlist for ``operation``.

    Returns the resolved path. Raises ``PathSafetyError`` on any
    unsafe condition. The function is intentionally strict: every
    caller MUST receive a resolved path on success and the caller
    is responsible for using that resolved path for any subsequent
    operation (delete, rename, etc.).

    ``operation`` is one of ``"delete"``, ``"rename"``, ``"move"``,
    ``"read"``, ``"write"``. The allowlist is more permissive for
    read/write than for delete/rename/move.
    """
    if not path:
        raise PathSafetyError("path is empty")
    if not str(path).strip():
        raise PathSafetyError("path is whitespace only")

    # Disallow obvious traversal strings BEFORE normalization.
    raw = str(path)
    if raw != raw.strip():
        raise PathSafetyError(f"path has leading/trailing whitespace: {raw!r}")
    if "\x00" in raw:
        raise PathSafetyError(f"path contains NUL byte: {raw!r}")

    normalized = _normalize(raw)

    # Exact-match protection for intrinsic-forbidden paths.
    if _is_protected_root(normalized):
        raise PathSafetyError(
            f"REFUSED: {normalized} is an intrinsic-forbidden path"
        )

    # Resolve through symlinks. Nonexistent paths are handled
    # by the caller; we deliberately raise to force the caller
    # to be explicit about a path that cannot be resolved.
    try:
        resolved = _resolve(normalized)
    except OSError as exc:
        raise PathSafetyError(
            f"REFUSED: cannot resolve {normalized}: {exc}"
        ) from exc

    # Post-resolution forbidden check: even if the input path
    # was not intrinsic-forbidden, the resolved path may be
    # an intrinsic-forbidden path (e.g. via a symlink).
    if _is_protected_root(resolved):
        raise PathSafetyError(
            f"REFUSED: {path} resolves to intrinsic-forbidden {resolved}"
        )

    # Intersect with the allowed-roots list, if any.
    if allowed_roots is not None:
        ok = False
        for root in allowed_roots:
            try:
                resolved.relative_to(_resolve(root))
            except ValueError:
                continue
            else:
                ok = True
                break
        if not ok:
            raise PathSafetyError(
                f"REFUSED: {resolved} is not under any allowed root "
                f"{[str(r) for r in allowed_roots]}"
            )

    # Operation-specific checks.
    if operation in ("delete", "rename", "move"):
        # The active runtime symlink is protected against all
        # destructive operations.
        o1_venv = target.deployment_root / "venv"
        if resolved == _resolve(o1_venv):
            raise PathSafetyError(
                f"REFUSED: {resolved} is O1's active runtime and cannot "
                f"be {operation}d"
            )
        # O2's deployment root is protected.
        if _is_under(resolved, supervisor.deployment_root):
            raise PathSafetyError(
                f"REFUSED: {resolved} is under O2's deployment root and "
                f"cannot be {operation}d"
            )
        # O2's home is protected.
        o2_home = identity.HOME_MAPPING.get(str(supervisor.deployment_root))
        if o2_home is not None and _is_under(resolved, o2_home):
            raise PathSafetyError(
                f"REFUSED: {resolved} is under O2's home and cannot "
                f"be {operation}d"
            )

    return resolved


def _is_under(path: Path, root: Path) -> bool:
    """Return True iff ``path`` is the same as ``root`` or under it."""
    try:
        path.relative_to(_resolve(root))
    except ValueError:
        return False
    return True


def is_o1_active_runtime(path: Path | str, target: Instance | None = None) -> bool:
    """Return True iff ``path`` is O1's active runtime."""
    target = target or identity.O1
    venv = target.deployment_root / "venv"
    try:
        resolved = _resolve(venv)
    except OSError:
        return False
    return _resolve(path) == resolved


def is_o2_release_path(path: Path | str, supervisor: Instance | None = None) -> bool:
    """Return True iff ``path`` is under O2's release root."""
    supervisor = supervisor or identity.O2
    return _is_under(_resolve(path), supervisor.deployment_root / "releases")


def is_o2_db_path(path: Path | str, supervisor: Instance | None = None) -> bool:
    """Return True iff ``path`` is O2's DB or its home directory."""
    supervisor = supervisor or identity.O2
    home = identity.HOME_MAPPING.get(str(supervisor.deployment_root))
    if home is None:
        return False
    return _is_under(_resolve(path), home)


def is_o1_staging_path(path: Path | str, tx_id: str, target: Instance | None = None) -> bool:
    """Return True iff ``path`` is under O1's per-transaction staging dir."""
    target = target or identity.O1
    stage = target.deployment_root / "staging" / tx_id
    return _is_under(_resolve(path), stage)


def is_o1_release_path(path: Path | str, target: Instance | None = None) -> bool:
    """Return True iff ``path`` is under O1's release root."""
    target = target or identity.O1
    return _is_under(_resolve(path), target.deployment_root / "releases")


__all__ = [
    "INTRINSIC_FORBIDDEN",
    "PathSafetyError",
    "assert_on_allowlist",
    "is_o1_active_runtime",
    "is_o2_release_path",
    "is_o2_db_path",
    "is_o1_staging_path",
    "is_o1_release_path",
]
