"""Verification for the current immutable ``acceptance-v2.json`` format.

This module contains the path-independent artifact checks shared by the RTX
peer controller and the disposable single-service prototype. It knows
nothing about O1/O2 identities or service topology.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import subprocess
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

_SHA = re.compile(r"^[a-f0-9]{40}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")


class AcceptedArtifactError(RuntimeError):
    """An acceptance-v2 record or immutable release failed verification."""


def canonical_digest(value: Mapping[str, Any]) -> str:
    """Return the canonical JSON digest used by acceptance-v2 records."""

    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def file_digest(path: Path) -> str:
    """Hash one regular artifact file without following an indirect path."""

    metadata = path.lstat()
    if not stat.S_ISREG(metadata.st_mode):
        raise AcceptedArtifactError(f"accepted artifact is not a regular file: {path}")
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def _default_runner(args: list[str]) -> str:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        cwd="/tmp",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
        timeout=120,
    )
    if result.returncode:
        raise AcceptedArtifactError(
            f"accepted runtime check failed: {args[0]} (rc={result.returncode})"
        )
    return result.stdout.strip()


def load_acceptance_record(
    acceptance_path: Path,
    *,
    artifacts_root: Path,
    expected_digest: str | None = None,
    owner_uid: int = 0,
    trust: Callable[[Path], None] | None = None,
) -> dict[str, Any]:
    """Load and canonically pin one trusted acceptance-v2 record."""

    if trust is None:

        def local_trust(path: Path) -> None:
            _local_trust(path, root=artifacts_root, owner_uid=owner_uid)

        trust = local_trust
    trust(acceptance_path)
    try:
        record = json.loads(acceptance_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AcceptedArtifactError("acceptance-v2 record cannot be read") from exc
    if not isinstance(record, dict):
        raise AcceptedArtifactError("acceptance-v2 record must be a JSON object")
    digest = canonical_digest(record)
    if expected_digest is not None and digest != expected_digest:
        raise AcceptedArtifactError("accepted-artifact digest mismatch")
    source_sha = record.get("source_sha")
    if not isinstance(source_sha, str) or not _SHA.fullmatch(source_sha):
        raise AcceptedArtifactError("acceptance-v2 source SHA is invalid")
    if acceptance_path != artifacts_root / source_sha / "acceptance-v2.json":
        raise AcceptedArtifactError("wrong canonical acceptance-v2 path")
    return record


def _local_trust(path: Path, *, root: Path, owner_uid: int) -> None:
    """Check a path and its components under one configured artifact root."""

    root = root.resolve(strict=True)
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise AcceptedArtifactError(f"trusted path escaped its root: {path}") from exc
    item = root
    components = [root]
    for part in relative.parts:
        item = item / part
        components.append(item)
    for item in components:
        metadata = item.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise AcceptedArtifactError(f"trusted path contains a symlink: {item}")
        if metadata.st_uid != owner_uid or metadata.st_mode & 0o022:
            raise AcceptedArtifactError(f"trusted path has unsafe owner/mode: {item}")


def verify_accepted_artifact(
    acceptance_path: Path,
    expected_digest: str,
    *,
    artifacts_root: Path,
    release_root: Path,
    owner_uid: int = 0,
    bound_release_root: Path | None = None,
    trust: Callable[[Path], None] | None = None,
    runner: Callable[[list[str]], str] = _default_runner,
    uv_executable: str | None = None,
    run_uv_check: bool = True,
) -> dict[str, Any]:
    """Verify one v2 record and the exact release bytes it names.

    ``bound_release_root`` is normally identical to ``release_root``. A
    disposable test may copy accepted bytes beneath temporary roots while
    preserving the record's original immutable-root identity; the copy is
    then verified against that pinned origin path.
    """

    if not _SHA256.fullmatch(expected_digest):
        raise AcceptedArtifactError("acceptance digest must be a lowercase SHA-256")

    def local_trust(path: Path) -> None:
        for root in (artifacts_root, release_root):
            try:
                path.relative_to(root.resolve(strict=True))
            except ValueError:
                continue
            _local_trust(path, root=root, owner_uid=owner_uid)
            return
        raise AcceptedArtifactError(f"trusted path escaped configured roots: {path}")

    trust_path = trust or local_trust
    record = load_acceptance_record(
        acceptance_path,
        artifacts_root=artifacts_root,
        expected_digest=expected_digest,
        owner_uid=owner_uid,
        trust=trust_path,
    )

    source_sha = record.get("source_sha")
    assert isinstance(source_sha, str)

    release = release_root / source_sha
    pinned_root = bound_release_root or release_root
    expected_runtime = pinned_root / source_sha
    if record.get("runtime") != str(expected_runtime):
        raise AcceptedArtifactError("acceptance-v2 names a different immutable runtime")
    expected_python = expected_runtime / "venv/bin/python"
    if record.get("python") != str(expected_python):
        raise AcceptedArtifactError("acceptance-v2 names a different runtime interpreter")
    if release.is_symlink() or not release.is_dir():
        raise AcceptedArtifactError(
            f"accepted immutable runtime is missing or indirect: {release}"
        )
    trust_path(release)

    if record.get("schema_policy") != "same-schema":
        raise AcceptedArtifactError("unsupported acceptance schema policy")
    schema = record.get("schema")
    if not isinstance(schema, str) or not schema:
        raise AcceptedArtifactError("acceptance-v2 schema identity is missing")
    checks = record.get("checks")
    if not isinstance(checks, dict) or not all(
        checks.get(name) is True
        for name in (
            "dependencies",
            "isolated_boot",
            "build_identity",
            "frontend",
            "o3_off",
            "smart_routing",
        )
    ):
        raise AcceptedArtifactError("acceptance-v2 candidate checks are incomplete")
    info = record.get("info")
    if not isinstance(info, dict):
        raise AcceptedArtifactError("acceptance-v2 runtime info evidence is missing")
    if info.get("build_sha") is not None and info.get("build_sha") != source_sha:
        raise AcceptedArtifactError("acceptance-v2 info build SHA differs from its source SHA")

    hashes = record.get("hashes")
    if not isinstance(hashes, dict) or not hashes:
        raise AcceptedArtifactError("acceptance-v2 contains no immutable byte hashes")
    for relative_name, expected_hash in hashes.items():
        if (
            not isinstance(relative_name, str)
            or not relative_name
            or not isinstance(expected_hash, str)
            or not _SHA256.fullmatch(expected_hash)
        ):
            raise AcceptedArtifactError("acceptance-v2 contains an invalid byte-hash entry")
        relative = Path(relative_name)
        if relative.is_absolute() or ".." in relative.parts:
            raise AcceptedArtifactError("acceptance-v2 contains an unsafe relative path")
        resource = release / relative
        if not resource.is_file() or resource.is_symlink():
            raise AcceptedArtifactError(
                f"accepted runtime resource is missing or indirect: {relative}"
            )
        trust_path(resource)
        if file_digest(resource) != expected_hash:
            raise AcceptedArtifactError(f"accepted runtime resource digest changed: {relative}")

    python = release / "venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        raise AcceptedArtifactError("accepted runtime Python is missing or not executable")
    resolved_python = python.resolve(strict=True)
    try:
        resolved_python.relative_to(release.resolve())
    except ValueError:
        metadata = resolved_python.stat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022:
            raise AcceptedArtifactError(
                "accepted runtime Python symlink target is untrusted"
            ) from None
    embedded_sha = runner(
        [
            str(python),
            "-c",
            "from omnigent._build_info import COMMIT_SHA; print(COMMIT_SHA)",
        ]
    )
    if embedded_sha != source_sha:
        raise AcceptedArtifactError("accepted runtime embedded build SHA mismatch")
    if info.get("server_version") and record.get("package_version"):
        if info["server_version"] != record["package_version"]:
            raise AcceptedArtifactError("acceptance-v2 package version evidence differs")

    if run_uv_check:
        if not uv_executable:
            raise AcceptedArtifactError(
                "uv executable missing for accepted runtime dependency check"
            )
        runner([uv_executable, "pip", "check", "--python", str(python)])
    return record


__all__ = [
    "AcceptedArtifactError",
    "canonical_digest",
    "file_digest",
    "load_acceptance_record",
    "verify_accepted_artifact",
]
