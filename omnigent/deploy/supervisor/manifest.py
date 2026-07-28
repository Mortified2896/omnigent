"""Manifest writer/reader for the long-term deploy architecture.

The promotion script (``scripts/promote_release.sh``) writes one
JSON file per promoted release into ``<deploy_root>/manifests/<sha>.json``.
The schema is intentionally small and human-readable so an operator
who looks at the wrong directory can still diagnose the situation from
``cat`` alone.

The manifest captures, at the moment of promotion:

* the canonical git commit SHA that the release was built from;
* an ISO-8601 UTC timestamp;
* the repository the SHA was resolved against;
* the release-local Python executable path and Python version string;
* the path ``omnigent`` resolved to inside the release, plus the path
  ``omnigent.server.app`` resolved to (so the supervisor check that
  fires at ``ExecStartPre`` time has a stable target to compare
  against);
* a sha256 fingerprint of the source ``uv.lock`` and
  ``web/package-lock.json`` (so a manifest written for one lockfile
  state can be cross-checked against a release directory whose lock
  was later patched out-of-band);
* the build version produced by the web bundler (``version.json``'s
  ``build`` field).

The supervisor gate only verifies two of these fields (commit and
provenance paths). The rest are diagnostic — useful to the deploy
status command, and durable enough to be the audit trail when a
promotion goes wrong.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

_MANIFEST_FILENAME = "manifest.json"


class ManifestError(RuntimeError):
    """Raised when the manifest is missing, malformed, or inconsistent.

    The supervisor gate distinguishes this from a generic
    ``RuntimeError`` so the user-facing error message in the
    systemd journal points at the exact reason
    (``manifest missing`` vs ``manifest SHA mismatch``).
    """


@dataclass
class ReleaseManifest:
    """One promoted release's manifest. See module docstring for the schema."""

    commit_sha: str
    built_at: str
    repository: str
    release_dir: str
    python_executable: str
    python_version: str
    omnigent_module_path: str
    omnigent_server_app_path: str
    frontend_build_version: str = ""
    lockfile_hashes: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_directory(
        cls,
        release_dir: Path,
        *,
        commit_sha: str,
        repository: str,
        python_executable: str,
        python_version: str,
        omnigent_module_path: str,
        omnigent_server_app_path: str,
        frontend_build_version: str = "",
        canonical_release_dir: Path | str | None = None,
    ) -> ReleaseManifest:
        """Build a manifest from the immutable archive and explicit SHA.

        ``commit_sha`` is resolved before archive extraction and is never
        inferred from the release directory.

        ``release_dir`` is the directory whose files are inspected and
        hashed (typically a staging directory that will be atomically
        renamed into the canonical release directory before this
        function returns). ``canonical_release_dir`` is the post-rename
        path the manifest records — i.e. the directory the release will
        live at once the promotion script's ``mv -T`` finalizes it.

        When ``canonical_release_dir`` is omitted, the manifest falls
        back to ``release_dir`` (the historical behavior); the staging
        flow always passes an explicit canonical path so the manifest
        survives the atomic rename intact.
        """
        if canonical_release_dir is None:
            canonical_path = Path(release_dir)
        elif isinstance(canonical_release_dir, str):
            canonical_path = Path(canonical_release_dir)
        else:
            canonical_path = canonical_release_dir
        return cls(
            commit_sha=commit_sha,
            built_at=_utcnow_iso(),
            repository=repository,
            release_dir=str(canonical_path.resolve()),
            python_executable=python_executable,
            python_version=python_version,
            omnigent_module_path=omnigent_module_path,
            omnigent_server_app_path=omnigent_server_app_path,
            frontend_build_version=frontend_build_version,
            lockfile_hashes=_collect_lockfile_hashes(release_dir),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True)


def manifest_path_for(release_dir: Path) -> Path:
    """Return the canonical manifest path inside ``release_dir/manifest.json``.

    The manifest lives inside the release directory (rather than in a
    parallel ``manifests/<sha>.json`` directory) so the release is
    self-describing — moving the directory moves its manifest with it.
    The promotion script also copies the manifest into the
    ``manifests/<sha>.json`` archive for ease of cross-deployment
    auditing.
    """
    return release_dir / "manifest.json"


def write_manifest(release_dir: Path, manifest: ReleaseManifest) -> Path:
    """Persist ``manifest`` to ``release_dir/manifest.json``.

    Writes atomically by writing to ``manifest.json.tmp`` and
    renaming. Refuses to overwrite an existing manifest unless
    ``OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE=1`` is set (the
    promotion script is the only thing that should write manifests,
    and a re-promotion of the same SHA is normally a no-op).
    """
    path = manifest_path_for(release_dir)
    if path.exists() and os.environ.get("OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE") != "1":
        raise ManifestError(
            f"manifest already exists at {path}; refusing to overwrite. "
            f"Set OMNIGENT_DEPLOY_ALLOW_MANIFEST_OVERWRITE=1 to force."
        )
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(manifest.to_json() + "\n", encoding="utf-8")
    os.replace(tmp, path)
    return path


def load_manifest(release_dir: Path) -> ReleaseManifest:
    """Load the manifest from ``release_dir/manifest.json``.

    Raises :class:`ManifestError` when the file is missing, unreadable,
    or contains an unexpected payload shape. The supervisor gate treats
    both cases as a hard startup failure (there is no fallback to
    "guess the SHA from the working directory").
    """
    path = manifest_path_for(release_dir)
    if not path.is_file():
        raise ManifestError(f"missing manifest at {path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ManifestError(f"manifest at {path} is not valid JSON: {exc}") from exc
    missing = {"commit_sha"} - set(data.keys())
    if missing:
        raise ManifestError(f"manifest at {path} is missing fields: {sorted(missing)}")
    return ReleaseManifest(
        commit_sha=data["commit_sha"],
        built_at=data.get("built_at", ""),
        repository=data.get("repository", ""),
        release_dir=data.get("release_dir", str(release_dir)),
        python_executable=data.get("python_executable", ""),
        python_version=data.get("python_version", ""),
        omnigent_module_path=data.get("omnigent_module_path", ""),
        omnigent_server_app_path=data.get("omnigent_server_app_path", ""),
        frontend_build_version=data.get("frontend_build_version", ""),
        lockfile_hashes=data.get("lockfile_hashes", {}) or {},
    )


def verify_manifest_commit(manifest: ReleaseManifest, expected_sha: str) -> None:
    """Refuse to consider a manifest authentic when ``commit_sha`` does
    not match ``expected_sha``.

    The supervisor gate calls this with the SHA recorded in
    ``OMNIGENT_RELEASE_EXPECTED_SHA`` (the canonical SHA written by the
    promotion script). A mismatch means the drop-in is pointing at a
    stale or misconfigured release.
    """
    if not expected_sha:
        raise ManifestError("expected_sha is empty")
    if manifest.commit_sha != expected_sha:
        raise ManifestError(
            f"manifest commit_sha={manifest.commit_sha} does not match expected_sha={expected_sha}"
        )


def verify_canonical_release_dir(
    manifest: ReleaseManifest,
    canonical_release_dir: Path | str,
) -> None:
    """Refuse to accept a manifest whose ``release_dir`` does not point at
    the canonical post-rename path.

    The promotion script builds in a staging directory
    (``releases/.staging-<sha>-...``) and atomically renames it into
    ``releases/<sha>``. If the manifest was written before the rename
    with the staging path baked in, ``manifest.release_dir`` would never
    match the canonical path that subsequent ``--build-only`` runs
    observe. This check pins that invariant so the staging-then-rename
    flow cannot regress to the "manifest records the staging path"
    defect.

    The comparison is against the *resolved* canonical path so a
    caller that passes either a symlink or a relative path converges
    with the resolved path the manifest itself stores.
    """
    canonical = Path(canonical_release_dir).resolve()
    recorded = Path(manifest.release_dir).resolve() if manifest.release_dir else None
    if recorded is None or recorded != canonical:
        raise ManifestError(
            f"manifest release_dir={recorded!s} does not match canonical "
            f"release_dir={canonical!s}; the manifest was likely written "
            "before the staging-to-canonical rename"
        )


# --- helpers -------------------------------------------------------------


def _utcnow_iso() -> str:
    """UTC timestamp in ISO-8601 with a 'Z' suffix.

    Using UTC ISO-8601 makes manifests sortable by string and timezone-
    stable when copied between hosts in a multi-region fleet.
    """
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _collect_lockfile_hashes(release_dir: Path) -> dict[str, str]:
    """Return a ``{relpath: sha256}`` map for the lockfiles in the release.

    Computed at manifest-write time so a later audit can prove the
    manifest pinned a specific lockfile state. The release directory's
    ``.venv/`` is excluded — the gate only cares about the build-time
    lockfiles, not the resolved wheel set inside the venv.
    """
    out: dict[str, str] = {}
    for rel in ("uv.lock", "web/package-lock.json"):
        path = release_dir / rel
        if not path.is_file():
            continue
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
        out[rel] = digest
    return out


def main() -> int:
    """CLI helper used by ``scripts/deploy_inspect_manifest.sh``.

    ``python -m omnigent.deploy.supervisor.manifest <release-dir>``
    pretty-prints the manifest, or prints ``(missing)`` on stderr and
    exits 1 when one is not present.
    """
    args = list(sys.argv[1:])
    if len(args) != 1:
        print(f"usage: {sys.argv[0]} <release-dir>", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest(Path(args[0]))
    except ManifestError as exc:
        print(f"(missing) {exc}", file=sys.stderr)
        return 1
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
