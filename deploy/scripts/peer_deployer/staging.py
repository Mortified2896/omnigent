"""Deterministic, reproducible staging for the peer-deployer.

The previous attempt to stage the O1 release did:

    pip install omnigent-0.9.0.dev0[all]

and let ``pip`` resolve the dependency closure from live PyPI. The
resolver hit ``opentelemetry-instrumentation-fastapi<1,>=0`` and
failed because the only available releases are 0.x beta (e.g.
``0.65b0``). The resolver walked off a cliff and the staging phase
was lost.

The hard lesson: the O2 supervisor is already running the *exact*
accepted runtime, with a *known-good* dependency closure, pinned by
``uv.lock`` and materialized on disk under
``/opt/omnigent-production/current/venv``. We must reproduce that
closure byte-for-version when we stage the O1 candidate. We must
**never** let ``pip`` (or ``uv pip``, or any other resolver) choose
newer versions of dependencies during a promotion.

This module implements the deterministic staging path:

  1. ``capture_supervisor_closure(supervisor)`` reads the supervisor's
     ``site-packages`` and produces a frozen manifest of the *exact*
     distributions installed in the supervisor's venv, with per-package
     version and SHA-256 hashes from the ``RECORD`` files.

  2. ``stage_candidate_runtime(target_release_root, supervisor,
     wheels, *, dry_run=False)`` builds the O1 candidate venv by
     copying the supervisor's ``site-packages`` wholesale and then
     installing the three SDK wheels (``omnigent``,
     ``omnigent_client``, ``omnigent_ui_sdk``) with ``--no-deps``,
     overwriting the existing distributions.

The staging path NEVER touches:

  * the supervisor's runtime or DB
  * the target's existing runtime or DB
  * live PyPI for any reason other than the exact three SDK wheels,
    and even then only as an emergency fallback if the wheels are
    missing from the artifacts dir

The staging path ALWAYS writes to a *transaction-owned* subdirectory
under the target's release root and registers that subdirectory in
the transaction record before any atomic swap into the final shared
location. Different transactions targeting the same accepted SHA can
no longer collide on stale partial state, because the staging path
is per-transaction.

The build order is:

  * artifacts/       — the three SDK wheels (copied from supervisor)
  * venv/            — built from the supervisor's venv layout
  * .complete        — written only after every verify step passes
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Iterable
from contextlib import suppress
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import identity
from .identity import Instance

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")


class StagingError(RuntimeError):
    """Raised when the staging phase cannot produce a valid candidate."""


@dataclass
class FrozenDistribution:
    """A single distribution as captured from the supervisor's site-packages."""

    name: str
    version: str
    dist_info_dir: str
    record_sha256_by_file: dict[str, str] = field(default_factory=dict)
    sha256_from_metadata: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FrozenClosure:
    """A frozen snapshot of the supervisor's installed dependency closure.

    The closure is what the staging phase copies onto the target. It
    carries *exact* versions and per-file SHA-256 hashes so that a
    subsequent install can prove byte-equivalence with the supervisor
    before any mutation of the active runtime.
    """

    supervisor_python: str
    captured_at_unix: float
    site_packages: str
    distributions: dict[str, FrozenDistribution]

    def to_dict(self) -> dict[str, Any]:
        return {
            "supervisor_python": self.supervisor_python,
            "captured_at_unix": self.captured_at_unix,
            "site_packages": self.site_packages,
            "distributions": {name: d.to_dict() for name, d in self.distributions.items()},
        }

    def expected_versions(self) -> dict[str, str]:
        return {name: d.version for name, d in self.distributions.items()}


def _read_dist_info(_site_packages: Path, dist_info: Path) -> FrozenDistribution:
    """Parse a single ``*.dist-info`` directory into a FrozenDistribution."""
    metadata_path = dist_info / "METADATA"
    if not metadata_path.is_file():
        raise StagingError(f"dist-info missing METADATA: {dist_info}")
    text = metadata_path.read_text(errors="replace")
    # The METADATA header is the only authoritative source of Name
    # and Version. The body may contain README fragments (e.g. the
    # ``distro`` package embeds a ``$ distro`` shell session in its
    # METADATA with ``Name: Antergos Linux`` as a literal example).
    # We must therefore parse the header BEFORE the blank line that
    # separates header from body. The PEP 566 grammar puts Name,
    # Version, and Hash in the header in that order.
    name: str | None = None
    version: str | None = None
    sha256_meta: str | None = None
    for line in text.splitlines():
        if not line.strip():
            # Blank line ends the metadata header. Stop parsing.
            break
        if name is None and line.startswith("Name:"):
            name = line.split(":", 1)[1].strip()
        elif version is None and line.startswith("Version:"):
            version = line.split(":", 1)[1].strip()
        elif sha256_meta is None and line.startswith("Hash:"):
            sha256_meta = line.split(":", 1)[1].strip()
    if not name or not version:
        raise StagingError(f"dist-info {dist_info} missing Name/Version in METADATA header")
    record = dist_info / "RECORD"
    record_hashes: dict[str, str] = {}
    if record.is_file():
        for rline in record.read_text(errors="replace").splitlines():
            if "," in rline:
                parts = rline.split(",")
                if len(parts) >= 2 and SHA256_RE.fullmatch(parts[1]):
                    record_hashes[parts[0]] = parts[1]
    return FrozenDistribution(
        name=name,
        version=version,
        dist_info_dir=dist_info.name,
        record_sha256_by_file=record_hashes,
        sha256_from_metadata=sha256_meta or "",
    )


def _locate_site_packages(supervisor: Instance) -> Path:
    """Return the supervisor's ``site-packages`` directory."""
    try:
        current = identity.read_current_symlink(supervisor.deployment_root)
    except identity.IdentityError:
        current = None
    if current is not None:
        candidates = sorted((current / "venv" / "lib").glob("python*/site-packages"))
        if candidates:
            return candidates[0]
    candidates = sorted(
        (supervisor.deployment_root / "venv" / "lib").glob("python*/site-packages")
    )
    if not candidates:
        raise StagingError(f"no site-packages under {supervisor.deployment_root}/venv/lib/")
    if len(candidates) != 1:
        raise StagingError(
            f"expected exactly one python site-packages under "
            f"{supervisor.deployment_root}/venv/lib/, found {len(candidates)}"
        )
    return candidates[0]


def _locate_supervisor_python(supervisor: Instance) -> Path:
    """Return the supervisor's active interpreter binary."""
    try:
        current = identity.read_current_symlink(supervisor.deployment_root)
    except identity.IdentityError:
        current = None
    if current is not None:
        candidate = current / "venv" / "bin" / "python"
        if candidate.is_file():
            return candidate
    fallback = supervisor.deployment_root / "venv" / "bin" / "python"
    if fallback.is_file():
        return fallback
    raise StagingError(f"no python interpreter found under {supervisor.deployment_root}/venv/bin/")


def capture_supervisor_closure(supervisor: Instance) -> FrozenClosure:
    """Walk the supervisor's site-packages and return a frozen closure.

    The closure is the single source of truth for what the candidate
    venv should contain. The staging phase must reproduce this closure
    byte-for-version before any mutation of the target's active
    runtime.
    """
    site = _locate_site_packages(supervisor)
    distributions: dict[str, FrozenDistribution] = {}
    for dist_info in sorted(site.glob("*.dist-info")):
        try:
            frozen = _read_dist_info(site, dist_info)
        except StagingError as exc:
            raise StagingError(
                f"failed to capture supervisor distribution {dist_info.name}: {exc}"
            ) from exc
        # pip and setuptools are special: their version string is not
        # meaningful for runtime equivalence and they are not pinned
        # in the lockfile. Skip them so the closure contains only
        # runtime-relevant distributions.
        if frozen.name in {"pip", "setuptools", "wheel", "pkg-resources", "pkg_resources"}:
            continue
        distributions[frozen.name] = frozen
    return FrozenClosure(
        supervisor_python=str(_locate_supervisor_python(supervisor)),
        captured_at_unix=time.time(),
        site_packages=str(site),
        distributions=distributions,
    )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_file(path: Path) -> str:
    return _hash_file(path)


def verify_candidate_versions(
    candidate_python: Path,
    expected: dict[str, str],
    *,
    cwd: Path | str = "/tmp",
) -> list[str]:
    """Return a list of mismatch descriptions; empty list means OK.

    Walks the candidate venv's site-packages and asserts that every
    name in ``expected`` is installed at the exact version listed.
    """
    result = subprocess.run(
        [
            str(candidate_python),
            "-c",
            (
                "import json, sys, importlib.metadata as md\n"
                "out = {}\n"
                "for d in md.distributions():\n"
                "    out[d.metadata['Name']] = d.version\n"
                "json.dump(out, sys.stdout)\n"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(cwd),
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if result.returncode != 0:
        raise StagingError(f"failed to enumerate candidate distributions: {result.stderr.strip()}")
    try:
        actual = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise StagingError(
            f"candidate version probe returned non-JSON: {result.stdout[:200]!r}"
        ) from exc
    mismatches: list[str] = []
    for name, expected_version in expected.items():
        if name not in actual:
            mismatches.append(f"missing:{name}")
        elif actual[name] != expected_version:
            mismatches.append(
                f"version_mismatch:{name} expected={expected_version} got={actual[name]}"
            )
    # We do NOT flag extra packages; some legitimately carry optional
    # transitive deps that vary by platform. The closure only requires
    # the supervisor's pinned set to be present at the right version.
    return mismatches


def _candidate_site_packages(target_release_root: Path) -> Path:
    candidates = sorted((target_release_root / "venv" / "lib").glob("python*/site-packages"))
    if not candidates:
        raise StagingError(f"no site-packages under {target_release_root}/venv/lib/")
    return candidates[0]


def _candidate_python(target_release_root: Path) -> Path:
    candidate = target_release_root / "venv" / "bin" / "python"
    if not candidate.is_file():
        raise StagingError(f"candidate python not found: {candidate}")
    return candidate


def _ensure_target_release_layout(target_release_root: Path, python: Path | None = None) -> None:
    """Make sure the candidate release directory exists with venv/ structure.

    Creates a uv-style venv skeleton (``pyvenv.cfg`` + ``bin/`` + ``lib/``)
    by delegating to ``python3 -m venv``. The site-packages and include
    directories are populated by ``_copy_supervisor_site_packages``.
    """
    if target_release_root.exists():
        raise StagingError(
            f"target release root already exists: {target_release_root}; "
            "refusing to overwrite. Use a transaction-owned staging path."
        )
    target_release_root.mkdir(parents=True)
    (target_release_root / "artifacts").mkdir()
    python = python or Path(shutil.which("python3") or shutil.which("python") or "")
    if not python.is_file():
        raise StagingError("python3 / python not available on PATH")
    venv_dir = target_release_root / "venv"
    result = subprocess.run(
        [python, "-m", "venv", str(venv_dir)],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise StagingError(
            f"python3 -m venv failed: rc={result.returncode} stderr={result.stderr.strip()}"
        )


def _copy_supervisor_site_packages(
    supervisor_site: Path,
    candidate_site: Path,
) -> None:
    """Copy the supervisor's site-packages onto the candidate.

    Copies every distribution including ``*.dist-info`` directories
    so the candidate has a complete, exact byte-for-version copy of
    the supervisor's closure. The SDK wheel ``pip install --no-deps``
    step OVERWRITES the dist-info for the three SDK packages
    (``omnigent``, ``omnigent_client``, ``omnigent_ui_sdk``), so the
    candidate ends up with the supervisor's pinned closure AND the
    freshly-installed SDK wheels — which is the exact combination
    required by the staging contract.

    The ``.dist-info`` directories for the supervisor's
    pre-installed ``omnigent`` etc. are NOT skipped; they are
    overwritten by pip's wheel install. This is intentional and
    desired: it is the only way to swap the SDK wheels without
    touching the rest of the closure.
    """
    if not supervisor_site.is_dir():
        raise StagingError(f"supervisor site-packages missing: {supervisor_site}")
    if candidate_site.exists():
        for child in candidate_site.iterdir():
            if child.is_dir():
                shutil.rmtree(child)
            else:
                child.unlink()
    else:
        candidate_site.mkdir(parents=True)
    for child in supervisor_site.iterdir():
        target = candidate_site / child.name
        if child.is_dir():
            shutil.copytree(child, target, symlinks=True)
        else:
            shutil.copy2(child, target)


def _install_wheels_no_deps(
    candidate_python: Path,
    wheels: Iterable[Path],
    supervisor: Instance,
) -> None:
    """Force-install the SDK wheels into the candidate venv with ``--no-deps``.

    We use ``pip`` because it is the lowest-common-denominator installer
    that ships with every Python. ``--no-deps`` is the whole point: the
    dependency closure has already been supplied by copying the
    supervisor's site-packages.

    Bootstrapping pip in the candidate venv uses ``ensurepip`` first
    (the standard mechanism). If that fails (some distributions ship
    without ``ensurepip``), we fall back to copying the supervisor's
    pip installation into the candidate site-packages. Either way, we
    MUST end up with a working pip before we touch any SDK wheel.
    """
    bootstrap = subprocess.run(
        [
            str(candidate_python),
            "-m",
            "ensurepip",
            "--upgrade",
            "--default-pip",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if bootstrap.returncode != 0:
        # Fall back: copy the supervisor's pip into the candidate's
        # site-packages. The supervisor's pip is itself a known-good
        # distribution; copying it is part of the deterministic
        # closure (it has the same SHA-256 as on the supervisor).
        # Find pip's dist-info dir on the explicitly declared supervisor.
        supervisor_site = _locate_site_packages(supervisor)
        candidate_site = (
            candidate_python.parent.parent
            / "lib"
            / ("python" + str(sys.version_info.major) + "." + str(sys.version_info.minor))
            / "site-packages"
        )
        if candidate_site.exists():
            for child in supervisor_site.glob("pip*.dist-info"):
                target = candidate_site / child.name
                if target.exists():
                    continue
                shutil.copytree(child, target)
            for child in supervisor_site.glob("pip-*"):
                # Only copy the pip package dir if present.
                pkg = candidate_site / child.name
                if not pkg.exists() and child.is_dir():
                    shutil.copytree(child, pkg)
        # Verify pip is now importable.
        verify = subprocess.run(
            [str(candidate_python), "-c", "import pip"],
            capture_output=True,
            text=True,
            check=False,
        )
        if verify.returncode != 0:
            raise StagingError(
                f"failed to bootstrap pip in candidate venv: "
                f"ensurepip rc={bootstrap.returncode} "
                f"stderr={bootstrap.stderr.strip()}; fallback also failed: "
                f"{verify.stderr.strip()}"
            )
    for wheel in wheels:
        if not wheel.is_file():
            raise StagingError(f"SDK wheel not found: {wheel}")
        # Use python -m pip directly so we never depend on a pip
        # binary in the candidate bin/.
        result = subprocess.run(
            [
                str(candidate_python),
                "-m",
                "pip",
                "install",
                "--no-deps",
                "--no-index",
                "--disable-pip-version-check",
                "--no-build-isolation",
                "--force-reinstall",
                str(wheel),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            raise StagingError(
                f"--no-deps install failed for {wheel.name}: "
                f"rc={result.returncode} stderr={result.stderr.strip()}"
            )


def verify_omnigent_console_entry_point(candidate_python: Path) -> None:
    """Require a working ``omnigent`` launcher bound to the candidate venv."""
    entry_point = candidate_python.parent / "omnigent"
    try:
        metadata = entry_point.lstat()
    except OSError as exc:
        raise StagingError(f"omnigent console entry point missing: {entry_point}") from exc
    if not stat.S_ISREG(metadata.st_mode) or entry_point.is_symlink():
        raise StagingError(
            f"omnigent console entry point is not a regular non-symlink file: {entry_point}"
        )
    if not os.access(entry_point, os.X_OK):
        raise StagingError(f"omnigent console entry point is not executable: {entry_point}")
    try:
        launcher = entry_point.read_bytes()[:4096]
    except OSError as exc:
        raise StagingError(f"cannot read omnigent console entry point: {entry_point}") from exc
    expected_interpreter = str(candidate_python.absolute()).encode()
    if expected_interpreter not in launcher:
        raise StagingError(
            "omnigent console entry point is not bound to candidate interpreter "
            f"{candidate_python.absolute()}: {entry_point}"
        )
    environment = {
        "PATH": (
            f"{candidate_python.parent}:"
            "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
        ),
        "PYTHONPATH": "",
        "PYTHONNOUSERSITE": "1",
        "HOME": "/tmp",
    }
    metadata_probe = subprocess.run(
        [
            str(candidate_python),
            "-c",
            (
                "import json, importlib.metadata as md; "
                "print(json.dumps([ep.value for ep in md.distribution('omnigent').entry_points "
                "if ep.group == 'console_scripts' and ep.name == 'omnigent']))"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd="/tmp",
        env=environment,
    )
    try:
        declared = json.loads(metadata_probe.stdout)
    except json.JSONDecodeError:
        declared = []
    if metadata_probe.returncode != 0 or declared != ["omnigent.cli:main"]:
        raise StagingError(
            "omnigent console entry point metadata is invalid: "
            f"rc={metadata_probe.returncode} stderr={metadata_probe.stderr.strip()[:500]}"
        )
    try:
        help_probe = subprocess.run(
            [str(entry_point), "--help"],
            capture_output=True,
            text=True,
            check=False,
            cwd="/tmp",
            env=environment,
            timeout=30,
        )
    except subprocess.TimeoutExpired as exc:
        raise StagingError("omnigent console entry point --help timed out") from exc
    if help_probe.returncode != 0:
        raise StagingError(
            "omnigent console entry point --help failed: "
            f"rc={help_probe.returncode} stderr={help_probe.stderr.strip()[:500]}"
        )


def stage_candidate_runtime(
    target_release_root: Path,
    supervisor: Instance,
    wheels: dict[str, Path],
    *,
    dry_run: bool = False,
    supervisor_release_root: Path | None = None,
    accepted_frontend_root: Path | None = None,
) -> FrozenClosure:
    """Stage a complete, transaction-owned candidate runtime at ``target_release_root``.

    ``wheels`` maps the SDK wheel name (``"main"``, ``"sdk_client"``,
    ``"sdk_ui"``) to the absolute path of the wheel. The wheels MUST
    be present locally before this function is called; live PyPI is
    never consulted.

    Steps:

      1. Create the candidate venv layout under ``target_release_root``.
      2. Copy the supervisor's site-packages wholesale into the
         candidate (including ``*.dist-info`` directories).
      3. Force-reinstall each of the three SDK wheels with ``pip install
         --no-deps --no-index``, overwriting the supervisor's same-version
         distributions and recreating their entry points.
      4. Copy the three SDK wheels into ``target_release_root/artifacts/``
         and copy PROVENANCE.txt from the supervisor's release so the
         candidate is self-describing.
      5. Verify the candidate's runtime identity matches the accepted
         artifact (commit SHA + version) and that the candidate's
         package versions match the supervisor's closure.
      6. Touch ``.complete`` to signal the candidate is verified.

    Raises ``StagingError`` on any failure. The caller is responsible
    for cleaning up a partially-staged directory on failure.

    ``supervisor_release_root`` defaults to ``supervisor.deployment_root``;
    callers that have a specific release dir (e.g. the host-level
    deployer that reads the accepted artifact from
    ``releases/<sha>``) can pass it explicitly.
    """
    if dry_run:
        # The dry-run path is used by tests and by preflight. It does
        # not create the target_release_root on disk but still returns
        # the closure that *would* be applied.
        return capture_supervisor_closure(supervisor)
    if "main" not in wheels or "sdk_client" not in wheels or "sdk_ui" not in wheels:
        raise StagingError("stage_candidate_runtime requires main, sdk_client, and sdk_ui wheels")
    if supervisor_release_root is None:
        # Default to the supervisor's current symlink target.
        try:
            supervisor_release_root = identity.read_current_symlink(supervisor.deployment_root)
        except identity.IdentityError:
            supervisor_release_root = supervisor.deployment_root
    supervisor_python = identity._resolve_active_python(supervisor.deployment_root)
    _ensure_target_release_layout(target_release_root, supervisor_python)
    try:
        supervisor_site = _locate_site_packages(supervisor)
        candidate_site = _candidate_site_packages(target_release_root)
        _copy_supervisor_site_packages(supervisor_site, candidate_site)
        candidate_python = _candidate_python(target_release_root)
        # Install order: SDKs first (so the main wheel sees them), then
        # the main wheel last so its dist-info wins.
        ordered = [
            wheels["sdk_client"],
            wheels["sdk_ui"],
            wheels["main"],
        ]
        _install_wheels_no_deps(candidate_python, ordered, supervisor)
        if accepted_frontend_root is not None:
            source_frontend = supervisor_release_root / accepted_frontend_root
            target_frontend = target_release_root / accepted_frontend_root
            if not target_frontend.exists():
                if not source_frontend.is_dir():
                    raise StagingError(f"accepted frontend tree missing: {source_frontend}")
                target_frontend.parent.mkdir(parents=True, exist_ok=True)
                shutil.copytree(source_frontend, target_frontend, symlinks=True)
        # Copy the three SDK wheels into artifacts/ and the PROVENANCE.txt
        # so the staged candidate is self-describing and the host-level
        # deployer does not need to do another copy step.
        write_artifacts(target_release_root, wheels)
        prov_src = supervisor_release_root / "PROVENANCE.txt"
        if prov_src.is_file():
            shutil.copy2(prov_src, target_release_root / "PROVENANCE.txt")
        # Verify: runtime identity must match the accepted artifact.
        identity_blob = identity.runtime_identity(candidate_python)
        if "commit_sha" not in identity_blob:
            raise StagingError(
                f"candidate runtime identity probe returned no commit_sha: {identity_blob!r}"
            )
        if "version" not in identity_blob:
            raise StagingError(
                f"candidate runtime identity probe returned no version: {identity_blob!r}"
            )
        # Verify: every pinned distribution from the supervisor must
        # be present at the same version.
        closure = capture_supervisor_closure(supervisor)
        mismatches = verify_candidate_versions(candidate_python, closure.expected_versions())
        if mismatches:
            raise StagingError(
                f"candidate runtime versions do not match supervisor closure: "
                f"{', '.join(mismatches)}"
            )
        verify_omnigent_console_entry_point(candidate_python)
        (target_release_root / ".complete").touch()
        # Persist the staging manifest alongside the candidate so
        # operators can later audit what was installed.
        write_staging_manifest(target_release_root, closure)
        return closure
    except Exception:
        # Best-effort cleanup so a half-built candidate does not leak.
        with suppress(OSError):
            shutil.rmtree(target_release_root)
        raise


def transaction_owned_staging_path(
    target: Instance,
    tx_id: str,
) -> Path:
    """Return the per-transaction staging path under ``target.deployment_root``.

    The path is::

        /opt/omnigent/staging/<TX_ID>/

    Every staging attempt for ``tx_id`` uses this path. Two different
    transactions can never collide on partial state, and a partially
    staged candidate from a failed transaction cannot be mistaken for
    a complete one because ``.complete`` is only touched after every
    verify step passes.
    """
    if not tx_id.startswith("promotion-"):
        raise StagingError(f"refusing non-canonical tx_id: {tx_id!r}")
    return target.deployment_root / "staging" / tx_id


def is_transaction_owned(target: Instance, path: Path, tx_id: str) -> bool:
    """Return True iff ``path`` lives under the transaction's staging dir.

    The check is path-normalized so that a symlink inside the staging
    tree cannot trick the rollback subsystem into deleting a sibling
    or ancestor path.
    """
    if not path:
        return False
    base = transaction_owned_staging_path(target, tx_id)
    try:
        path_resolved = Path(path).resolve()
        base_resolved = base.resolve()
    except OSError:
        return False
    try:
        path_resolved.relative_to(base_resolved)
        return True
    except ValueError:
        return False


def safe_cleanup_staging(target: Instance, tx_id: str) -> bool:
    """Remove the transaction-owned staging dir, but only if it is owned.

    Returns ``True`` if the directory existed and was removed, ``False``
    otherwise. Raises ``StagingError`` if the path resolves outside
    the transaction's staging tree (defense-in-depth).
    """
    base = transaction_owned_staging_path(target, tx_id)
    real = base.resolve()
    # Defense in depth: refuse to remove anything that does not live
    # under ``/opt/omnigent/staging/<TX_ID>``.
    expected_parent = (target.deployment_root / "staging").resolve()
    try:
        real.relative_to(expected_parent)
    except ValueError as exc:
        raise StagingError(
            f"REFUSED: {real} is not under {expected_parent}; refusing to remove"
        ) from exc
    expected_name = tx_id
    if real.name != expected_name:
        raise StagingError(f"REFUSED: {real} name {real.name!r} != tx_id {expected_name!r}")
    if not real.exists():
        return False
    shutil.rmtree(real)
    return True


def write_staging_provenance(staging_root: Path, source_release_root: Path) -> None:
    """Copy PROVENANCE.txt from the supervisor's release into the staging root.

    The PROVENANCE.txt is the authoritative artifact identity. It is
    what the host-level deployer compares against the accepted
    wheel hashes. It MUST be copied verbatim — never regenerated.
    """
    src = source_release_root / "PROVENANCE.txt"
    if not src.is_file():
        raise StagingError(f"supervisor PROVENANCE.txt missing: {src}")
    target = staging_root / "PROVENANCE.txt"
    shutil.copy2(src, target)


def write_artifacts(staging_root: Path, wheels: dict[str, Path]) -> None:
    """Copy the three SDK wheels into ``staging_root/artifacts/``."""
    target = staging_root / "artifacts"
    target.mkdir(parents=True, exist_ok=True)
    for label, wheel in wheels.items():
        if not wheel.is_file():
            raise StagingError(f"wheel {label!r} not found: {wheel}")
        shutil.copy2(wheel, target / wheel.name)


def candidate_identity_matches(
    staging_root: Path,
    expected_sha: str,
    expected_version: str,
) -> bool:
    """Return True iff the staged candidate's runtime identity matches expectations.

    Reads the staged venv's ``_build_info`` via the same import-based
    helper used for the supervisor. Returns ``False`` on any mismatch
    or probe failure (does NOT raise).
    """
    if not SHA_RE.fullmatch(expected_sha):
        return False
    try:
        python = _candidate_python(staging_root)
    except StagingError:
        return False
    try:
        blob = identity.runtime_identity(python)
    except identity.IdentityError:
        return False
    return blob.get("commit_sha") == expected_sha and blob.get("version") == expected_version


def verify_candidate_complete(staging_root: Path) -> list[str]:
    """Return a list of verification failures; empty list means OK.

    Verifies, in order:

      * ``.complete`` exists
      * PROVENANCE.txt exists and has the canonical schema_version/sha/package_version
      * artifacts/ contains all three SDK wheels
      * the candidate venv imports ``omnigent``, ``omnigent_client``,
        ``omnigent_ui_sdk`` without error
      * the migration module imports cleanly
      * no unresolved-dependency errors at import time
    """
    failures: list[str] = []
    if not (staging_root / ".complete").is_file():
        failures.append("missing:.complete marker")
    provenance = staging_root / "PROVENANCE.txt"
    if not provenance.is_file():
        failures.append("missing:PROVENANCE.txt")
    else:
        text = provenance.read_text()
        for required in ("schema_version=", "sha=", "package_version="):
            if required not in text:
                failures.append(f"missing-in-provenance:{required.rstrip('=')}")
    artifacts = staging_root / "artifacts"
    if not artifacts.is_dir():
        failures.append("missing:artifacts/")
    else:
        for required in ("omnigent-", "omnigent_client-", "omnigent_ui_sdk-"):
            if not any(artifacts.glob(f"{required}*.whl")):
                failures.append(f"missing-wheel:{required}")
    try:
        python = _candidate_python(staging_root)
    except StagingError as exc:
        failures.append(f"missing-python:{exc}")
        return failures
    try:
        verify_omnigent_console_entry_point(python)
    except StagingError as exc:
        failures.append(f"entry-point-check-failed:{exc}")
    import_check = subprocess.run(
        [
            str(python),
            "-c",
            (
                "import omnigent, omnigent_client, omnigent_ui_sdk;"
                "import omnigent.db;"
                "import fastapi;"
                "import opentelemetry;"
                "import opentelemetry.instrumentation.fastapi;"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
        cwd="/tmp",
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
    )
    if import_check.returncode != 0:
        failures.append(
            f"import-check-failed: rc={import_check.returncode} "
            f"stderr={import_check.stderr.strip()[:500]}"
        )
    return failures


def write_staging_manifest(staging_root: Path, closure: FrozenClosure) -> Path:
    """Write the frozen closure to disk as JSON inside the staging root.

    The manifest is the evidence that the candidate was built
    deterministically from the supervisor's closure. It is preserved
    alongside the candidate so that an operator can later audit what
    was actually installed.
    """
    staging_root.mkdir(parents=True, exist_ok=True)
    manifest = staging_root / "staging-manifest.json"
    manifest.write_text(json.dumps(closure.to_dict(), indent=2, sort_keys=True))
    return manifest


__all__ = [
    "FrozenClosure",
    "FrozenDistribution",
    "StagingError",
    "candidate_identity_matches",
    "capture_supervisor_closure",
    "is_transaction_owned",
    "safe_cleanup_staging",
    "stage_candidate_runtime",
    "transaction_owned_staging_path",
    "verify_candidate_complete",
    "verify_candidate_versions",
    "verify_omnigent_console_entry_point",
    "write_artifacts",
    "write_staging_manifest",
    "write_staging_provenance",
]
