"""Capture reproducible Git workspace state for future benchmark construction."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any


class BenchmarkCaptureError(RuntimeError):
    """Raised when a benchmark workspace snapshot cannot be captured safely."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "-C", str(repo), *args],
            check=check,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise BenchmarkCaptureError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise BenchmarkCaptureError(
            f"git {' '.join(args)} failed{f': {detail}' if detail else ''}"
        ) from exc


def _git_text(repo: Path, *args: str, check: bool = True) -> str | None:
    result = _git(repo, *args, check=check)
    if not check and result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="strict").strip()
    return value or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _safe_untracked_path(raw: str) -> Path:
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts or rel == Path("."):
        raise BenchmarkCaptureError(f"unsafe untracked path returned by git: {raw!r}")
    return rel


def snapshot_git_state(repo: Path, destination: Path) -> dict[str, Any]:
    """Capture a read-only, reconstructable snapshot of one Git worktree.

    The repository itself is never mutated. ``destination`` must live outside
    the repository so the capture cannot become part of the state it records.
    """

    requested_repo = repo.resolve()
    root_raw = _git_text(requested_repo, "rev-parse", "--show-toplevel")
    if root_raw is None:
        raise BenchmarkCaptureError(f"{requested_repo} is not a Git worktree")
    root = Path(root_raw).resolve()

    dest = destination.resolve(strict=False)
    if dest == root or dest.is_relative_to(root):
        raise BenchmarkCaptureError("benchmark capture destination must be outside the repository")

    head = _git_text(root, "rev-parse", "HEAD")
    if head is None:
        raise BenchmarkCaptureError("repository has no resolvable HEAD")

    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    origin = _git_text(root, "config", "--get", "remote.origin.url", check=False)

    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    staged = _git(root, "diff", "--cached", "--binary", "--full-index", "--no-ext-diff").stdout
    unstaged = _git(root, "diff", "--binary", "--full-index", "--no-ext-diff").stdout
    untracked_raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = [
        _safe_untracked_path(item.decode("utf-8", errors="surrogateescape"))
        for item in untracked_raw.split(b"\0")
        if item
    ]

    dest.mkdir(parents=True, exist_ok=False)

    status_path = dest / "status.z"
    staged_path = dest / "staged.patch"
    unstaged_path = dest / "unstaged.patch"
    untracked_path = dest / "untracked.tar"

    status_path.write_bytes(status)
    staged_path.write_bytes(staged)
    unstaged_path.write_bytes(unstaged)

    try:
        with tarfile.open(untracked_path, mode="w", dereference=False) as archive:
            for rel in untracked:
                source = root / rel
                if not source.exists() and not source.is_symlink():
                    raise BenchmarkCaptureError(
                        f"untracked path disappeared during capture: {rel.as_posix()}"
                    )
                archive.add(source, arcname=rel.as_posix(), recursive=False)
    except Exception:
        # A partial archive must never masquerade as a complete snapshot.
        untracked_path.unlink(missing_ok=True)
        raise

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "repo_root": str(root),
        "origin": origin,
        "head": head,
        "branch": branch,
        "detached": branch is None,
        "untracked_paths": [path.as_posix() for path in untracked],
        "artifacts": {
            "status": _artifact(status_path),
            "staged_patch": _artifact(staged_path),
            "unstaged_patch": _artifact(unstaged_path),
            "untracked_archive": _artifact(untracked_path),
        },
    }
    (dest / "git.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata
