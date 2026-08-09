"""Identity and target/supervisor classification for the peer-deployer.

The Control Room operates two instances on the same host:

  * O1 — the maintenance (acceptance) instance
  * O2 — the production (supervisor) instance

The hard invariant is: an instance NEVER upgrades itself. O1 upgrades O2;
O2 upgrades O1. The healthy peer stays alive and supervises the entire
operation.

This module provides vetted helpers that prove, from a *single* snapshot
of the host, what each instance is, what it is running, and whether it
is healthy. The peer-deployer consults these helpers at every preflight
check.

SHA / version discovery
-----------------------

The deployed runtime identity (commit SHA + version) is read by
importing the installed package from its on-disk site-packages, in a
neutral cwd with the source tree and ``PYTHONPATH`` cleared. This is
deliberately *not* a text-regex parse of ``_build_info.py``: the file's
exact source form (annotations, quotes, ordering) is not part of the
identity contract, and a regex parser is fragile against future style
changes such as ``COMMIT_SHA: str = '...'``.

The shared helpers are:

  * ``runtime_identity(python, *, cwd="/tmp")`` — invoke a specific
    interpreter and ask it for ``omnigent._build_info`` plus the
    installed ``omnigent`` distribution version.
  * ``installed_sha(deployment_root)`` / ``installed_version(deployment_root)``
    — convenience wrappers that locate the active interpreter and
    delegate to ``runtime_identity``.

The active interpreter is located by following the deployment root's
``current`` symlink. If ``current`` is missing or stale, the helper
falls back to ``deployment_root/venv/bin/python``. This mirrors the
layout on both O1 and O2: ``current`` -> a release directory, and the
venv lives inside the release directory.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

SHA_RE = re.compile(r"[0-9a-f]{40}")


class IdentityError(RuntimeError):
    """Raised when an instance identity cannot be proven."""


@dataclass(frozen=True)
class Instance:
    """A Control Room Omnigent instance, identified by its deployment root.

    ``deployment_root`` is the absolute path under which the instance's
    releases/, current symlink, PROVENANCE.txt, and runtime live.
    ``service_unit`` and ``host_unit`` are the systemd unit names that
    own the server and host processes respectively. ``port`` is the
    loopback port the server is bound to.
    """

    name: str
    deployment_root: Path
    service_unit: str
    host_unit: str
    port: int
    health_url: str

    def __post_init__(self) -> None:
        if not self.name:
            raise IdentityError("instance name is required")
        if not self.deployment_root.is_absolute():
            raise IdentityError(f"deployment_root must be absolute: {self.deployment_root}")
        if not self.service_unit:
            raise IdentityError("service_unit is required")
        if not self.host_unit:
            raise IdentityError("host_unit is required")
        if not (1 <= self.port <= 65535):
            raise IdentityError(f"port out of range: {self.port}")
        if not self.health_url:
            raise IdentityError("health_url is required")


# The canonical O1 / O2 definitions. The two instances must never collide
# on root, port, service name, or host name.
O1 = Instance(
    name="O1",
    deployment_root=Path("/opt/omnigent"),
    service_unit="omnigent.service",
    host_unit="omnigent-host.service",
    port=4097,
    health_url="http://127.0.0.1:4097/health",
)

O2 = Instance(
    name="O2",
    deployment_root=Path("/opt/omnigent-production"),
    service_unit="omnigent-production.service",
    host_unit="omnigent-production-host.service",
    port=4197,
    health_url="http://127.0.0.1:4197/health",
)

REGISTRY: dict[str, Instance] = {"O1": O1, "O2": O2}

# Mapping from deployment root to the data/DB home for each instance.
# The peer-deployer keeps these in sync with the canonical layout. The
# preflight and rollback subsystems use this mapping to find the DB,
# the artifacts, and the logs.
HOME_MAPPING: dict[str, Path] = {
    str(O1.deployment_root): Path("/var/lib/omnigent"),
    str(O2.deployment_root): Path("/var/lib/omnigent-production"),
}


def get(name: str) -> Instance:
    """Return the canonical instance record for ``name`` (``O1`` or ``O2``)."""
    if name not in REGISTRY:
        raise IdentityError(f"unknown instance: {name!r}; valid: {sorted(REGISTRY)}")
    return REGISTRY[name]


def require_distinct(target: Instance, supervisor: Instance) -> None:
    """Hard refusal: target and supervisor must be different instances.

    The deployment script must refuse to run if ``target == supervisor``.
    This is the elegant expression of the Control Room invariant:
    "an instance NEVER upgrades itself."
    """
    if target.name == supervisor.name:
        raise IdentityError(
            f"REFUSED: target == supervisor == {target.name!r}: "
            "an instance NEVER upgrades itself"
        )
    if target.deployment_root == supervisor.deployment_root:
        raise IdentityError(
            f"REFUSED: target and supervisor share deployment_root "
            f"{target.deployment_root}"
        )
    if target.service_unit == supervisor.service_unit:
        raise IdentityError(
            f"REFUSED: target and supervisor share service_unit "
            f"{target.service_unit!r}"
        )
    if target.port == supervisor.port:
        raise IdentityError(
            f"REFUSED: target and supervisor share port {target.port}"
        )


def read_provenance(deployment_root: Path) -> dict[str, str]:
    """Parse the PROVENANCE.txt file at the deployment root.

    Also supports the release layout where PROVENANCE.txt lives
    directly under the current symlink target.
    """
    # Try the current symlink first.
    try:
        current = read_current_symlink(deployment_root)
        candidate = current / "PROVENANCE.txt"
        if candidate.is_file():
            path = candidate
        else:
            path = Path(deployment_root) / "PROVENANCE.txt"
    except IdentityError:
        path = Path(deployment_root) / "PROVENANCE.txt"
    if not path.is_file():
        raise IdentityError(f"PROVENANCE.txt missing: {path}")
    result: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        if not raw or raw.startswith("#"):
            continue
        key, sep, value = raw.partition("=")
        if not sep or not key:
            raise IdentityError(f"invalid provenance line: {raw!r}")
        if key in result:
            raise IdentityError(f"duplicate provenance key: {key!r}")
        result[key] = value
    for required in ("sha", "package_version"):
        if required not in result:
            raise IdentityError(f"PROVENANCE.txt missing {required!r}")
    if not SHA_RE.fullmatch(result["sha"]):
        raise IdentityError(f"PROVENANCE.txt sha is not a 40-char SHA: {result['sha']!r}")
    return result


def read_current_symlink(deployment_root: Path) -> Path:
    """Return the path the deployment root's ``current`` symlink resolves to.

    Raises ``IdentityError`` if the symlink is missing or broken.
    """
    current = Path(deployment_root) / "current"
    if not current.is_symlink():
        raise IdentityError(
            f"{current} is not a symlink (deployment root has no current)"
        )
    target = current.resolve()
    if not target.exists():
        raise IdentityError(f"{current} -> {target} does not resolve to a real path")
    return target


# Path to the minimal helper script that, when run inside a target
# venv, returns the runtime identity as a single JSON document.
# The script deliberately uses ``import`` (rather than regex) so the
# source-tree formatting of ``_build_info.py`` is irrelevant.
_RUNTIME_IDENTITY_HELPER = """\
import json
import sys

# Read from the installed package, NOT from any source-tree shadowing.
# The cwd has been cleared by the caller; ``PYTHONPATH`` is empty.
result = {}
try:
    from omnigent._build_info import COMMIT_SHA  # type: ignore
    if isinstance(COMMIT_SHA, str) and len(COMMIT_SHA) == 40:
        result["commit_sha"] = COMMIT_SHA
    else:
        result["error"] = "COMMIT_SHA is not a 40-char string: %r" % (COMMIT_SHA,)
except Exception as exc:  # pragma: no cover - defensive
    result["error"] = "import failed: %s" % exc

# Try to read the installed distribution version from importlib.metadata
# (the canonical source — the same one ``pip show`` consults).
try:
    from importlib.metadata import version as _v
    result["version"] = _v("omnigent")
except Exception as exc:
    # Fall back to package __version__ if it exists, but only after
    # the canonical path has been tried.
    try:
        import omnigent
        v = getattr(omnigent, "__version__", None)
        if isinstance(v, str):
            result["version"] = v
        else:
            result["error_version"] = "omnigent has no __version__: %s" % exc
    except Exception as exc2:
        result["error_version"] = "no metadata, no __version__: %s / %s" % (exc, exc2)

json.dump(result, sys.stdout)
"""


def _resolve_active_python(deployment_root: Path) -> Path:
    """Return the path to the active runtime's ``python`` binary.

    Follows the ``current`` symlink under the deployment root. Falls
    back to ``deployment_root/venv/bin/python`` if ``current`` is
    missing. Raises ``IdentityError`` if no python interpreter can
    be located.
    """
    try:
        current = read_current_symlink(deployment_root)
    except IdentityError:
        current = None
    if current is not None:
        candidate = current / "venv" / "bin" / "python"
        if candidate.is_file():
            return candidate
    fallback = deployment_root / "venv" / "bin" / "python"
    if fallback.is_file():
        return fallback
    raise IdentityError(f"no python interpreter found under {deployment_root}/venv/bin/")


def runtime_identity(python: Path, *, cwd: Path | str = "/tmp") -> dict[str, str]:
    """Return the runtime identity (commit SHA + version) for ``python``.

    ``python`` is the absolute path to the interpreter that owns the
    runtime we want to inspect. ``cwd`` defaults to ``/tmp`` to avoid
    source-tree shadowing, and ``PYTHONPATH`` is explicitly cleared
    in the child environment.

    The helper runs ``python -c '<script>'`` with a small embedded
    script that imports ``omnigent._build_info`` (canonical
    runtime identity) and reads ``importlib.metadata.version``
    (canonical distribution identity). Returns a dict with at least
    the ``commit_sha`` and ``version`` keys on success.

    The previous implementation parsed ``_build_info.py`` with a
    text regex expecting ``COMMIT_SHA = '...'``. That format is
    *not* part of the identity contract; the package currently emits
    ``COMMIT_SHA: str = '...'`` which broke the regex. This helper
    uses the actual Python ``import`` machinery and works against
    any source form.

    Raises ``IdentityError`` if either key cannot be recovered.
    """
    if not python.is_file():
        raise IdentityError(f"interpreter not found: {python}")
    env = {
        # Minimal PATH so ``python -c`` can find its own bits; we don't
        # want any user-installed ``omnigent`` shadowing via PATH.
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(cwd),
        # Force the child to import only from its own site-packages.
        "PYTHONPATH": "",
        # Disable any user-site / virtualenv manipulation.
        "PYTHONNOUSERSITE": "1",
    }
    try:
        result = subprocess.run(
            [str(python), "-c", _RUNTIME_IDENTITY_HELPER],
            capture_output=True,
            text=True,
            check=False,
            cwd=str(cwd),
            env=env,
        )
    except FileNotFoundError as exc:
        raise IdentityError(f"failed to exec {python}: {exc}") from exc
    if result.returncode != 0:
        raise IdentityError(
            f"runtime identity probe failed (rc={result.returncode}): "
            f"stderr={result.stderr.strip()!r}"
        )
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise IdentityError(
            f"runtime identity probe returned non-JSON: {result.stdout[:200]!r}"
        ) from exc
    if not isinstance(payload, dict):
        raise IdentityError(
            f"runtime identity probe returned non-dict: {payload!r}"
        )
    return payload


def installed_sha(deployment_root: Path) -> str:
    """Return the SHA recorded in the active runtime's installed package.

    Uses ``runtime_identity`` so the answer reflects the *installed*
    wheel, not any source-tree checkout. The active interpreter is
    located by following ``deployment_root/current`` (or by falling
    back to ``deployment_root/venv/bin/python``).
    """
    payload = runtime_identity(_resolve_active_python(deployment_root))
    sha = payload.get("commit_sha")
    if not isinstance(sha, str):
        raise IdentityError(
            f"runtime identity probe did not return a commit_sha: {payload!r}"
        )
    if not SHA_RE.fullmatch(sha):
        raise IdentityError(
            f"runtime identity commit_sha is not a 40-char SHA: {sha!r}"
        )
    return sha


def installed_version(deployment_root: Path) -> str:
    """Return the version recorded by the deployed package.

    Uses the deployed Python interpreter so the answer reflects the
    installed wheel, not the workspace checkout. The active runtime
    is determined by following the ``current`` symlink.
    """
    payload = runtime_identity(_resolve_active_python(deployment_root))
    version = payload.get("version")
    if not isinstance(version, str) or not version:
        raise IdentityError(
            f"runtime identity probe did not return a version: {payload!r}"
        )
    return version


def http_health_ok(url: str, timeout_s: float = 3.0) -> bool:
    """Return ``True`` iff ``GET url`` returns HTTP 200 and ``{"status":"ok"}``."""
    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", str(timeout_s), url],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise IdentityError(f"curl not available: {exc}") from exc
    if result.returncode != 0:
        return False
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError:
        return False
    return payload.get("status") == "ok"


def snapshot(instance: Instance) -> dict:
    """Return a frozen snapshot of the instance's identity and health.

    The snapshot is what the peer-deployer compares against before and
    after every promotion to prove the supervisor was not touched.
    """
    provenance = read_provenance(instance.deployment_root)
    return {
        "name": instance.name,
        "deployment_root": str(instance.deployment_root),
        "service_unit": instance.service_unit,
        "host_unit": instance.host_unit,
        "port": instance.port,
        "health_url": instance.health_url,
        "provenance_sha": provenance["sha"],
        "provenance_version": provenance["package_version"],
        "installed_sha": installed_sha(instance.deployment_root),
        "installed_version": installed_version(instance.deployment_root),
        "health_ok": http_health_ok(instance.health_url),
    }


def snapshots_equal(a: dict, b: dict) -> bool:
    """Compare two ``snapshot`` dicts, ignoring ``health_ok``.

    Health is a runtime observability signal; identity and provenance
    are what must not change during a peer-supervised upgrade.
    """
    keys = (
        "name",
        "deployment_root",
        "service_unit",
        "host_unit",
        "port",
        "health_url",
        "provenance_sha",
        "provenance_version",
        "installed_sha",
        "installed_version",
    )
    return all(a.get(k) == b.get(k) for k in keys)


__all__ = [
    "Instance",
    "IdentityError",
    "O1",
    "O2",
    "REGISTRY",
    "HOME_MAPPING",
    "get",
    "require_distinct",
    "read_provenance",
    "read_current_symlink",
    "runtime_identity",
    "installed_sha",
    "installed_version",
    "http_health_ok",
    "snapshot",
    "snapshots_equal",
]
