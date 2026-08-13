"""Deterministic, versioned, immutable candidate acceptance records.

The embedded ``acceptance_record_sha256`` is the SHA-256 of the canonical
JSON payload with that field omitted. This avoids a self-referential digest
while binding every acceptance assertion. The complete record is stored at
``accepted-artifacts/<source-sha>/acceptance.json`` and is never replaced.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import identity

SCHEMA_VERSION = 1
DEFAULT_ACCEPTANCE_ROOT = Path("/var/lib/omnigent-control-room/accepted-artifacts")
TEMPORARY_PORT_BOOT_CLASSIFICATION = "isolated-temporary-port"
_SHA_RE = re.compile(r"[0-9a-f]{40}")
_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_IDENTITY_RE = re.compile(r"[A-Za-z0-9_.@:-]{1,128}")
_REQUIRED_WHEEL_ROLES = frozenset({"main", "sdk_client", "sdk_ui"})
_REQUIRED_PACKAGES = frozenset({"omnigent", "omnigent_client", "omnigent_ui_sdk"})


class AcceptanceError(RuntimeError):
    """Raised when acceptance evidence is invalid or mutable."""


@dataclass(frozen=True, order=True)
class AcceptedWheel:
    role: str
    filename: str
    sha256: str

    def validate(self) -> None:
        if self.role not in _REQUIRED_WHEEL_ROLES:
            raise AcceptanceError(f"unknown wheel role: {self.role!r}")
        if not self.filename or Path(self.filename).name != self.filename:
            raise AcceptanceError(f"wheel filename must be a basename: {self.filename!r}")
        if not self.filename.endswith(".whl"):
            raise AcceptanceError(f"accepted artifact is not a wheel: {self.filename!r}")
        if not _SHA256_RE.fullmatch(self.sha256):
            raise AcceptanceError(f"invalid wheel SHA-256 for {self.filename!r}")

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> AcceptedWheel:
        if set(blob) != {"role", "filename", "sha256"}:
            raise AcceptanceError(f"invalid accepted wheel keys: {sorted(blob)}")
        value = cls(**blob)
        value.validate()
        return value


@dataclass(frozen=True, order=True)
class InstalledPackage:
    name: str
    version: str
    path: str

    def validate(self, release_root: Path) -> None:
        if self.name not in _REQUIRED_PACKAGES or not self.version:
            raise AcceptanceError(f"invalid installed package: {self.name!r}")
        path = Path(self.path)
        if not path.is_absolute() or ".." in path.parts:
            raise AcceptanceError(f"installed package path is unsafe: {path}")
        try:
            path.relative_to(release_root)
        except ValueError as exc:
            raise AcceptanceError(
                f"installed package path is outside immutable release: {path}"
            ) from exc

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> InstalledPackage:
        if set(blob) != {"name", "version", "path"}:
            raise AcceptanceError(f"invalid installed package keys: {sorted(blob)}")
        return cls(**blob)


@dataclass(frozen=True)
class CandidateAcceptance:
    schema_version: int
    source_sha: str
    package_version: str
    wheels: tuple[AcceptedWheel, ...]
    frontend_root: str
    frontend_tree_sha256: str
    immutable_release_root: str
    runtime_venv_path: str
    installed_packages: tuple[InstalledPackage, ...]
    uv_pip_check_success: bool
    embedded_build_sha: str
    boot_command_classification: str
    temporary_port: int
    health_ok: bool
    health_status: str
    info_ok: bool
    info_server_version: str
    info_build_sha: str
    html_assets_ok: bool
    html_asset_count: int
    disk_headroom_bytes: int
    accepted_at: str
    builder_identity: str
    operator_identity: str
    target_db_schema: str
    acceptance_record_sha256: str

    @property
    def artifact_sha(self) -> str:
        """Compatibility name used by transaction and staging code."""
        return self.source_sha

    @property
    def artifact_version(self) -> str:
        return self.package_version

    @property
    def candidate_path(self) -> str:
        return self.immutable_release_root

    @property
    def frontend_sha256(self) -> str:
        return self.frontend_tree_sha256

    def validate(self, *, validate_digest: bool = True) -> None:
        if self.schema_version != SCHEMA_VERSION:
            raise AcceptanceError(
                f"unsupported acceptance schema {self.schema_version}; expected {SCHEMA_VERSION}"
            )
        if not _SHA_RE.fullmatch(self.source_sha):
            raise AcceptanceError("source_sha must be a lowercase 40-character SHA")
        if not self.package_version:
            raise AcceptanceError("package_version is required")
        release = Path(self.immutable_release_root)
        runtime = Path(self.runtime_venv_path)
        frontend = Path(self.frontend_root)
        if not release.is_absolute() or ".." in release.parts:
            raise AcceptanceError("immutable_release_root must be absolute without traversal")
        if release.name != self.source_sha:
            raise AcceptanceError("immutable_release_root basename must equal source_sha")
        if not runtime.is_absolute() or ".." in runtime.parts:
            raise AcceptanceError("runtime_venv_path must be absolute without traversal")
        try:
            runtime.relative_to(release)
        except ValueError as exc:
            raise AcceptanceError("runtime_venv_path must be inside immutable release") from exc
        if frontend.is_absolute() or not self.frontend_root or ".." in frontend.parts:
            raise AcceptanceError("frontend_root must be relative without traversal")
        if not _SHA256_RE.fullmatch(self.frontend_tree_sha256):
            raise AcceptanceError("frontend_tree_sha256 must be a lowercase SHA-256")
        if tuple(sorted(self.wheels)) != self.wheels:
            raise AcceptanceError("wheels must be in deterministic sorted order")
        roles = [item.role for item in self.wheels]
        if set(roles) != _REQUIRED_WHEEL_ROLES or len(roles) != 3:
            raise AcceptanceError("exactly one main, sdk_client, and sdk_ui wheel is required")
        for wheel in self.wheels:
            wheel.validate()
        if tuple(sorted(self.installed_packages)) != self.installed_packages:
            raise AcceptanceError("installed_packages must be deterministically sorted")
        names = [item.name for item in self.installed_packages]
        if set(names) != _REQUIRED_PACKAGES or len(names) != 3:
            raise AcceptanceError("all three installed packages must be recorded exactly once")
        for package in self.installed_packages:
            package.validate(release)
            if package.version != self.package_version:
                raise AcceptanceError(
                    f"installed {package.name} version differs from accepted package version"
                )
        if self.uv_pip_check_success is not True:
            raise AcceptanceError("acceptance requires a successful uv pip check")
        if self.embedded_build_sha != self.source_sha:
            raise AcceptanceError("embedded build SHA differs from source SHA")
        if self.boot_command_classification != TEMPORARY_PORT_BOOT_CLASSIFICATION:
            raise AcceptanceError(
                "candidate was not booted with isolated temporary-port classification"
            )
        if not (1024 <= self.temporary_port <= 65535):
            raise AcceptanceError("temporary_port must be an unprivileged TCP port")
        if not self.health_ok or self.health_status != "ok":
            raise AcceptanceError("successful candidate health evidence is required")
        if not self.info_ok:
            raise AcceptanceError("successful /v1/info evidence is required")
        if self.info_server_version != self.package_version:
            raise AcceptanceError("/v1/info version differs from package_version")
        if self.info_build_sha != self.source_sha:
            raise AcceptanceError("/v1/info build SHA differs from source_sha")
        if not self.html_assets_ok or self.html_asset_count < 1:
            raise AcceptanceError("successful HTML/assets evidence is required")
        if self.disk_headroom_bytes <= 0:
            raise AcceptanceError("positive disk headroom evidence is required")
        if not self.target_db_schema:
            raise AcceptanceError("target_db_schema is required")
        try:
            accepted_at = datetime.fromisoformat(self.accepted_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise AcceptanceError("accepted_at must be an ISO-8601 timestamp") from exc
        if accepted_at.tzinfo is None:
            raise AcceptanceError("accepted_at must include a timezone")
        for label, value in (
            ("builder_identity", self.builder_identity),
            ("operator_identity", self.operator_identity),
        ):
            if not _IDENTITY_RE.fullmatch(value):
                raise AcceptanceError(f"{label} contains unsafe or secret-like content")
        if validate_digest:
            if not _SHA256_RE.fullmatch(self.acceptance_record_sha256):
                raise AcceptanceError("acceptance_record_sha256 is invalid")
            actual = payload_sha256(self)
            if self.acceptance_record_sha256 != actual:
                raise AcceptanceError("acceptance_record_sha256 does not match canonical payload")

    def to_dict(self, *, include_record_hash: bool = True) -> dict[str, Any]:
        self.validate(validate_digest=include_record_hash)
        result: dict[str, Any] = {
            "schema_version": self.schema_version,
            "source_sha": self.source_sha,
            "package_version": self.package_version,
            "wheels": [asdict(item) for item in self.wheels],
            "frontend_root": self.frontend_root,
            "frontend_tree_sha256": self.frontend_tree_sha256,
            "immutable_release_root": self.immutable_release_root,
            "runtime_venv_path": self.runtime_venv_path,
            "installed_packages": [asdict(item) for item in self.installed_packages],
            "uv_pip_check_success": self.uv_pip_check_success,
            "embedded_build_sha": self.embedded_build_sha,
            "boot_command_classification": self.boot_command_classification,
            "temporary_port": self.temporary_port,
            "health_ok": self.health_ok,
            "health_status": self.health_status,
            "info_ok": self.info_ok,
            "info_server_version": self.info_server_version,
            "info_build_sha": self.info_build_sha,
            "html_assets_ok": self.html_assets_ok,
            "html_asset_count": self.html_asset_count,
            "disk_headroom_bytes": self.disk_headroom_bytes,
            "accepted_at": self.accepted_at,
            "builder_identity": self.builder_identity,
            "operator_identity": self.operator_identity,
            "target_db_schema": self.target_db_schema,
        }
        if include_record_hash:
            result["acceptance_record_sha256"] = self.acceptance_record_sha256
        return result

    @classmethod
    def create(cls, **values: Any) -> CandidateAcceptance:
        values["schema_version"] = SCHEMA_VERSION
        values["wheels"] = tuple(sorted(values["wheels"]))
        values["installed_packages"] = tuple(sorted(values["installed_packages"]))
        values["acceptance_record_sha256"] = ""
        draft = cls(**values)
        draft.validate(validate_digest=False)
        record = replace(draft, acceptance_record_sha256=payload_sha256(draft))
        record.validate()
        return record

    @classmethod
    def from_dict(cls, blob: dict[str, Any]) -> CandidateAcceptance:
        fields = set(cls.__dataclass_fields__)
        if set(blob) != fields:
            raise AcceptanceError(f"acceptance keys must be {sorted(fields)}; got {sorted(blob)}")
        values = dict(blob)
        raw_wheels = values.pop("wheels")
        raw_packages = values.pop("installed_packages")
        if not isinstance(raw_wheels, list) or not isinstance(raw_packages, list):
            raise AcceptanceError("wheels and installed_packages must be lists")
        record = cls(
            wheels=tuple(AcceptedWheel.from_dict(item) for item in raw_wheels),
            installed_packages=tuple(InstalledPackage.from_dict(item) for item in raw_packages),
            **values,
        )
        record.validate()
        return record

    def wheel_map(self, release_root: Path | None = None) -> dict[str, Path]:
        root = release_root or Path(self.immutable_release_root)
        return {item.role: root / "artifacts" / item.filename for item in self.wheels}


def _json_bytes(blob: dict[str, Any]) -> bytes:
    return (json.dumps(blob, sort_keys=True, separators=(",", ":")) + "\n").encode()


def payload_sha256(record: CandidateAcceptance) -> str:
    """Hash canonical payload excluding ``acceptance_record_sha256``."""
    return hashlib.sha256(_json_bytes(record.to_dict(include_record_hash=False))).hexdigest()


def canonical_bytes(record: CandidateAcceptance) -> bytes:
    return _json_bytes(record.to_dict())


def record_sha256(record_or_bytes: CandidateAcceptance | bytes) -> str:
    """Return the embedded payload digest, validating complete bytes if given."""
    if isinstance(record_or_bytes, CandidateAcceptance):
        record_or_bytes.validate()
        return record_or_bytes.acceptance_record_sha256
    try:
        blob = json.loads(record_or_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"invalid acceptance bytes: {exc}") from exc
    record = CandidateAcceptance.from_dict(blob)
    if record_or_bytes != canonical_bytes(record):
        raise AcceptanceError("acceptance bytes are not canonical")
    return record.acceptance_record_sha256


def canonical_path(record: CandidateAcceptance, *, root: Path = DEFAULT_ACCEPTANCE_ROOT) -> Path:
    return Path(root) / record.source_sha / "acceptance.json"


def write_immutable(record: CandidateAcceptance, *, root: Path = DEFAULT_ACCEPTANCE_ROOT) -> Path:
    """Exclusively create the canonical record; never replace existing bytes."""
    path = canonical_path(record, root=root)
    payload = canonical_bytes(record)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    except FileExistsError:
        if path.read_bytes() != payload:
            raise AcceptanceError(f"immutable acceptance record differs: {path}") from None
        return path
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(path, 0o444)
    except BaseException:
        with suppress(OSError):
            path.unlink()
        raise
    return path


def verify_record_permissions(path: Path | str) -> None:
    """Require the live acceptance record to be root-owned and read-only."""
    candidate = Path(path)
    try:
        metadata = candidate.lstat()
    except OSError as exc:
        raise AcceptanceError(f"cannot stat acceptance record {candidate}: {exc}") from exc
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        raise AcceptanceError(f"acceptance record must be a regular non-symlink file: {candidate}")
    if metadata.st_uid != 0:
        raise AcceptanceError(f"acceptance record is not root-owned: {candidate}")
    if metadata.st_mode & 0o222:
        raise AcceptanceError(f"acceptance record is writable: {candidate}")
    root = DEFAULT_ACCEPTANCE_ROOT
    try:
        candidate.relative_to(root)
    except ValueError as exc:
        raise AcceptanceError(f"acceptance record is outside canonical root: {candidate}") from exc
    for parent in (root, *candidate.parents[: len(candidate.parents) - len(root.parents)]):
        parent_metadata = parent.lstat()
        if stat.S_ISLNK(parent_metadata.st_mode) or parent_metadata.st_uid != 0:
            raise AcceptanceError(f"acceptance parent is not root-owned/non-symlink: {parent}")
        if parent_metadata.st_mode & 0o022:
            raise AcceptanceError(f"acceptance parent is group/world writable: {parent}")


def load(
    path: Path | str,
    *,
    expected_hash: str | None = None,
    require_immutable_permissions: bool = False,
) -> CandidateAcceptance:
    candidate = Path(path)
    if require_immutable_permissions:
        verify_record_permissions(candidate)
    try:
        payload = candidate.read_bytes()
    except OSError as exc:
        raise AcceptanceError(f"cannot read acceptance record {candidate}: {exc}") from exc
    try:
        blob = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AcceptanceError(f"invalid acceptance JSON {candidate}: {exc}") from exc
    if not isinstance(blob, dict):
        raise AcceptanceError("acceptance record must be a JSON object")
    record = CandidateAcceptance.from_dict(blob)
    if payload != canonical_bytes(record):
        raise AcceptanceError(f"acceptance record is not canonical: {candidate}")
    if expected_hash is not None and record.acceptance_record_sha256 != expected_hash:
        raise AcceptanceError(f"acceptance record hash mismatch: {candidate}")
    return record


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def tree_sha256(root: Path) -> str:
    """Hash a frontend tree by relative path, kind, mode, and file digest."""
    if not root.is_dir():
        raise AcceptanceError(f"frontend tree missing: {root}")
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        stat = path.lstat()
        if path.is_symlink():
            resolved = path.resolve()
            try:
                resolved.relative_to(root.resolve())
            except ValueError as exc:
                raise AcceptanceError(f"frontend symlink escapes accepted tree: {path}") from exc
            entries.append({"path": relative, "kind": "symlink", "target": os.readlink(path)})
        elif path.is_dir():
            entries.append({"path": relative, "kind": "directory", "mode": stat.st_mode & 0o777})
        elif path.is_file():
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": stat.st_mode & 0o777,
                    "sha256": sha256_file(path),
                }
            )
        else:
            raise AcceptanceError(f"unsupported frontend tree entry: {path}")
    return hashlib.sha256(_json_bytes({"entries": entries})).hexdigest()


def _installed_package_probe(python: Path) -> dict[str, dict[str, str]]:
    script = (
        "import importlib, importlib.metadata as m, json\n"
        "out={}\n"
        "for n in ('omnigent','omnigent_client','omnigent_ui_sdk'):\n"
        " mod=importlib.import_module(n)\n"
        " out[n]={'version':m.version(n.replace('_','-')), 'path':mod.__file__}\n"
        "print(json.dumps(out,sort_keys=True))\n"
    )
    result = subprocess.run(
        [str(python), "-c", script],
        cwd="/tmp",
        env={
            "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            "PYTHONPATH": "",
            "PYTHONNOUSERSITE": "1",
        },
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise AcceptanceError(f"installed package probe failed: {result.stderr.strip()}")
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise AcceptanceError("installed package probe returned invalid JSON") from exc


def verify_release(
    record: CandidateAcceptance,
    release_root: Path,
    *,
    run_uv_check: bool = True,
    enforce_bound_root: bool = True,
) -> list[str]:
    """Re-check immutable resources, runtime imports, identity, and boot evidence."""
    failures: list[str] = []
    try:
        record.validate()
    except AcceptanceError as exc:
        return [f"acceptance invalid:{exc}"]
    bound_root = Path(record.immutable_release_root)
    if enforce_bound_root and release_root != bound_root:
        failures.append(f"release root:{release_root} != immutable {bound_root}")
    if not release_root.is_dir() or release_root.is_symlink():
        return [*failures, f"immutable release missing or symlinked:{release_root}"]
    resolved_release = release_root.resolve()
    for path in (release_root, *release_root.rglob("*")):
        metadata = path.lstat()
        if metadata.st_uid != 0:
            failures.append(f"release resource not root-owned:{path}")
        if metadata.st_mode & 0o022:
            failures.append(f"release resource group/world writable:{path}")
        if path.is_symlink():
            try:
                path.resolve().relative_to(resolved_release)
            except ValueError:
                failures.append(f"release symlink escapes immutable root:{path}")
    for wheel in record.wheels:
        path = release_root / "artifacts" / wheel.filename
        if not path.is_file():
            failures.append(f"wheel missing:{path}")
        elif sha256_file(path) != wheel.sha256:
            failures.append(f"wheel hash mismatch:{path}")
    frontend = release_root / record.frontend_root
    try:
        actual_tree = tree_sha256(frontend)
    except AcceptanceError as exc:
        failures.append(str(exc))
    else:
        if actual_tree != record.frontend_tree_sha256:
            failures.append(f"frontend tree hash mismatch:{frontend}")
    recorded_venv = Path(record.runtime_venv_path)
    if enforce_bound_root:
        venv = recorded_venv
    else:
        try:
            venv = release_root / recorded_venv.relative_to(bound_root)
        except ValueError:
            failures.append("runtime venv cannot be relocated from immutable release")
            return failures
    python = venv / "bin" / "python"
    if not python.is_file() or not os.access(python, os.X_OK):
        failures.append(f"runtime python missing/not executable:{python}")
        return failures
    try:
        runtime = identity.runtime_identity(python)
    except identity.IdentityError as exc:
        failures.append(f"runtime identity:{exc}")
    else:
        if runtime.get("commit_sha") != record.source_sha:
            failures.append("runtime embedded SHA mismatch")
        if runtime.get("version") != record.package_version:
            failures.append("runtime package version mismatch")
    try:
        observed = _installed_package_probe(python)
    except AcceptanceError as exc:
        failures.append(str(exc))
    else:
        for expected in record.installed_packages:
            actual = observed.get(expected.name, {})
            if actual.get("version") != expected.version:
                failures.append(f"installed version mismatch:{expected.name}")
            try:
                actual_path = str(Path(actual.get("path", "")).resolve())
                recorded_path = Path(expected.path)
                if enforce_bound_root:
                    relocated_path = recorded_path
                else:
                    relocated_path = release_root / recorded_path.relative_to(bound_root)
                expected_path = str(relocated_path.resolve())
            except (OSError, ValueError):
                actual_path, expected_path = "", expected.path
            if actual_path != expected_path:
                failures.append(f"installed path mismatch:{expected.name}")
    if run_uv_check:
        uv = shutil.which("uv")
        if uv is None:
            failures.append("uv executable missing for uv pip check")
        else:
            checked = subprocess.run(
                [uv, "pip", "check", "--python", str(python)],
                cwd="/tmp",
                env={
                    "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
                    "PYTHONPATH": "",
                    "PYTHONNOUSERSITE": "1",
                },
                capture_output=True,
                text=True,
                check=False,
            )
            if checked.returncode:
                failures.append(f"uv pip check failed:{checked.stderr.strip()}")
    # validate() binds successful isolated boot, health, info, and HTML/assets
    # evidence. No command text or environment is persisted, avoiding secrets.
    return failures


__all__ = [
    "DEFAULT_ACCEPTANCE_ROOT",
    "SCHEMA_VERSION",
    "TEMPORARY_PORT_BOOT_CLASSIFICATION",
    "AcceptanceError",
    "AcceptedWheel",
    "CandidateAcceptance",
    "InstalledPackage",
    "canonical_bytes",
    "canonical_path",
    "load",
    "payload_sha256",
    "record_sha256",
    "sha256_file",
    "tree_sha256",
    "verify_record_permissions",
    "verify_release",
    "write_immutable",
]
