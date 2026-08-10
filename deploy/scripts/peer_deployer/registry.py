"""Root-owned trusted registry of immutable Control Room artifacts.

The trusted registry is the *only* place where an artifact identity
(SHA + version + wheel hashes + provenance) is bound to a verified
on-disk source.  Service-level callers address registry entries by
``artifact_sha`` only — they NEVER pass an arbitrary filesystem path.

The registry file is owned by root with mode ``0600`` and lives at::

    /var/lib/control-room-peer-deployer/artifacts/registry.json

It is produced by the bootstrap installer and re-verified on every
service start.  The service **must not** fall back to any
application-level hardcoded artifact identity: the registry is the
single source of truth and is loaded once per daemon start.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

SHA_RE = re.compile(r"[0-9a-f]{40}")
SHA256_RE = re.compile(r"[0-9a-f]{64}")

REGISTRY_SCHEMA = "control-room-peer-deployer.trusted-artifact-registry.v1"


class RegistryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactEntry:
    artifact_sha: str
    version: str
    release_root: str
    provenance: str
    wheels: dict[str, dict[str, str]]  # name -> {"path": str, "sha256": str}

    def wheel_path(self, name_starts_with: str) -> str:
        for name, meta in self.wheels.items():
            if name.startswith(name_starts_with):
                return meta["path"]
        raise RegistryError(
            f"no wheel starting with {name_starts_with!r} in artifact {self.artifact_sha}"
        )


@dataclass(frozen=True)
class TrustedRegistry:
    release_digest: str
    supervisor_python: str
    artifacts: dict[str, ArtifactEntry] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, artifact_sha: str) -> ArtifactEntry:
        if artifact_sha not in self.artifacts:
            raise RegistryError(
                f"artifact {artifact_sha!r} not in trusted registry; only "
                f"{sorted(self.artifacts)} are root-approved"
            )
        return self.artifacts[artifact_sha]

    def has(self, artifact_sha: str) -> bool:
        return artifact_sha in self.artifacts


def _validate(path: Path, raw: dict[str, Any]) -> dict[str, ArtifactEntry]:
    if raw.get("schema") != REGISTRY_SCHEMA:
        raise RegistryError(
            f"registry schema mismatch at {path}: {raw.get('schema')!r}"
        )
    if not isinstance(raw.get("release_digest"), str) or not raw["release_digest"]:
        raise RegistryError(f"registry missing release_digest")
    if not isinstance(raw.get("supervisor_python"), str):
        raise RegistryError(f"registry missing supervisor_python")
    if not isinstance(raw.get("artifacts"), dict):
        raise RegistryError(f"registry missing artifacts dict")
    out: dict[str, ArtifactEntry] = {}
    for sha, blob in raw["artifacts"].items():
        if not SHA_RE.fullmatch(sha):
            raise RegistryError(f"registry key {sha!r} is not a 40-char SHA")
        if not isinstance(blob, dict):
            raise RegistryError(f"registry artifact {sha} is not a dict")
        version = blob.get("version")
        release_root = blob.get("release_root")
        provenance = blob.get("provenance")
        wheels = blob.get("wheels")
        if not isinstance(version, str) or not version:
            raise RegistryError(f"artifact {sha} missing version")
        if not isinstance(release_root, str) or not release_root:
            raise RegistryError(f"artifact {sha} missing release_root")
        if not isinstance(provenance, str) or not provenance:
            raise RegistryError(f"artifact {sha} missing provenance")
        if not isinstance(wheels, dict) or not wheels:
            raise RegistryError(f"artifact {sha} missing wheels")
        clean_wheels: dict[str, dict[str, str]] = {}
        for name, meta in wheels.items():
            if not isinstance(meta, dict):
                raise RegistryError(f"artifact {sha} wheel {name} is not a dict")
            p = meta.get("path")
            h = meta.get("sha256")
            if not isinstance(p, str) or not Path(p).is_absolute():
                raise RegistryError(
                    f"artifact {sha} wheel {name} must have an absolute path"
                )
            if not isinstance(h, str) or not SHA256_RE.fullmatch(h):
                raise RegistryError(
                    f"artifact {sha} wheel {name} must have a 64-char SHA-256"
                )
            clean_wheels[name] = {"path": p, "sha256": h}
        out[sha] = ArtifactEntry(
            artifact_sha=sha,
            version=version,
            release_root=release_root,
            provenance=provenance,
            wheels=clean_wheels,
        )
    return out


def load(path: Path | None = None) -> TrustedRegistry:
    """Load the trusted registry; refuse to proceed on any mismatch."""
    if path is None:
        path = Path("/var/lib/control-room-peer-deployer/artifacts/registry.json")
    if not path.is_file():
        raise RegistryError(f"trusted registry missing: {path}")
    blob = json.loads(path.read_text())
    if not isinstance(blob, dict):
        raise RegistryError(f"registry is not a JSON object: {path}")
    artifacts = _validate(path, blob)
    return TrustedRegistry(
        release_digest=blob["release_digest"],
        supervisor_python=blob["supervisor_python"],
        artifacts=artifacts,
        raw=blob,
    )


__all__ = [
    "ArtifactEntry",
    "RegistryError",
    "TrustedRegistry",
    "REGISTRY_SCHEMA",
    "load",
]
