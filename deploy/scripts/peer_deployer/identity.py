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
"""

from __future__ import annotations

import json
import re
import subprocess
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


def installed_sha(deployment_root: Path) -> str:
    """Return the SHA recorded in the active runtime's ``_build_info.py``.

    The active runtime is determined by following the ``current`` symlink
    under the deployment root, if any. If ``current`` is missing, the
    helper falls back to looking at the ``venv`` subdirectory directly.
    This mirrors the layout on both O1 and O2: ``current`` -> a release
    directory, and the venv lives inside the release directory.
    """
    # Try the current symlink first.
    try:
        current = read_current_symlink(deployment_root)
    except IdentityError:
        current = None
    if current is not None:
        site_packages = sorted((current / "venv" / "lib").glob("python*/site-packages"))
        if site_packages:
            return _parse_installed_sha(site_packages[0])
    # Fallback: deploy_root/venv/lib
    site_packages = sorted((deployment_root / "venv" / "lib").glob("python*/site-packages"))
    if not site_packages:
        raise IdentityError(f"no site-packages under {deployment_root}/venv/lib")
    if len(site_packages) != 1:
        raise IdentityError(
            f"expected exactly one python site-packages under {deployment_root}/venv/lib, "
            f"found {len(site_packages)}"
        )
    return _parse_installed_sha(site_packages[0])


def _parse_installed_sha(site_packages: Path) -> str:
    build_info = site_packages / "omnigent" / "_build_info.py"
    if not build_info.is_file():
        raise IdentityError(f"missing _build_info.py: {build_info}")
    tree = build_info.read_text()
    match = re.search(r"COMMIT_SHA[^\n]*?([0-9a-f]{40})", tree)
    if not match:
        raise IdentityError(f"could not parse COMMIT_SHA from {build_info}")
    return match.group(1)


def installed_version(deployment_root: Path) -> str:
    """Return the version recorded by the deployed package.

    Uses the deployed Python interpreter so the answer reflects the
    installed wheel, not the workspace checkout. The active runtime
    is determined by following the ``current`` symlink.
    """
    python = None
    try:
        current = read_current_symlink(deployment_root)
        candidate = current / "venv" / "bin" / "python"
        if candidate.is_file():
            python = candidate
    except IdentityError:
        pass
    if python is None:
        python = deployment_root / "venv" / "bin" / "python"
    if not python.is_file():
        raise IdentityError(f"missing python: {python}")
    result = subprocess.run(
        [str(python), "-c", "from omnigent.version import VERSION; print(VERSION)"],
        capture_output=True,
        text=True,
        check=False,
        cwd="/tmp",
        env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    if result.returncode != 0:
        raise IdentityError(
            f"failed to read installed version from {python}: {result.stderr.strip()}"
        )
    version = result.stdout.strip()
    if not version:
        raise IdentityError("installed version returned empty string")
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
    "installed_sha",
    "installed_version",
    "http_health_ok",
    "snapshot",
    "snapshots_equal",
]
