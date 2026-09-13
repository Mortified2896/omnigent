"""Fail-closed canonical repository identity checks.

The caller resolves the stable GitHub repository ID before invoking this
module. Local folder names and prompt text are never identity evidence.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

_GIT_TIMEOUT_S = 30.0
_MANIFEST = Path(__file__).resolve().parents[2] / "config/repository-ownership.json"
_SENTINEL = Path(".omnigent/repository.json")


class RepositoryIdentityError(ValueError):
    """Raised before mutation when repository identity cannot be proven."""


class RepositoryRole(StrEnum):
    """Canonical ownership roles accepted by the preflight."""

    OMNIGENT_PRODUCT_RUNTIME = "omnigent_product_runtime"
    HOMELAB_INFRASTRUCTURE = "homelab_infrastructure"
    OMNIROUTE_OVERLAYS = "omniroute_overlays"
    DORMANT_STANDALONE_ARCHIVE = "dormant_standalone_archive"


class ArchivalOverrideReason(StrEnum):
    """Narrow reasons that may inspect a dormant repository."""

    FORENSIC_INSPECTION = "forensic_inspection"
    PRESERVATION = "preservation"
    OWNER_AUTHORIZED_REVIVAL = "owner_authorized_revival"


@dataclass(frozen=True)
class RepositoryRecord:
    """One canonical repository entry from the ownership manifest."""

    repository_id: int
    owner: str
    name: str
    default_branch: str
    role: RepositoryRole
    state: str
    aliases: tuple[str, ...]

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


@dataclass(frozen=True)
class VerifiedRepositoryIdentity:
    """Identity/provenance fields safe to persist with a task."""

    repository_id: int
    full_name: str
    role: RepositoryRole
    state: str
    fetch_url: str
    push_url: str
    default_branch: str
    head_sha: str
    archival_override_reason: ArchivalOverrideReason | None


def _git(repo: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT_S,
            check=False,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        raise RepositoryIdentityError(f"repository identity git check failed: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip()
        raise RepositoryIdentityError(
            f"git {' '.join(args)} failed ({result.returncode}): {detail}"
        )
    return result.stdout.strip()


def _object(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RepositoryIdentityError(f"{label} must be a JSON object")
    return value


def load_manifest(path: Path = _MANIFEST) -> tuple[RepositoryRecord, ...]:
    """Load and validate the structured repository ownership manifest."""

    try:
        raw = _object(json.loads(path.read_text(encoding="utf-8")), label="manifest")
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryIdentityError(f"cannot load repository manifest: {exc}") from exc
    if raw.get("schema_version") != 1 or not isinstance(raw.get("repositories"), list):
        raise RepositoryIdentityError("unsupported repository manifest schema")
    records: list[RepositoryRecord] = []
    seen_ids: set[int] = set()
    seen_names: set[str] = set()
    for item in raw["repositories"]:
        entry = _object(item, label="repository entry")
        try:
            record = RepositoryRecord(
                repository_id=int(entry["repository_id"]),
                owner=str(entry["owner"]),
                name=str(entry["name"]),
                default_branch=str(entry["default_branch"]),
                role=RepositoryRole(str(entry["role"])),
                state=str(entry["state"]),
                aliases=tuple(str(alias) for alias in entry.get("aliases", [])),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RepositoryIdentityError(f"invalid repository manifest entry: {exc}") from exc
        names = {record.full_name.casefold(), *(alias.casefold() for alias in record.aliases)}
        if record.repository_id in seen_ids or names & seen_names:
            raise RepositoryIdentityError("duplicate repository ID or name in manifest")
        seen_ids.add(record.repository_id)
        seen_names.update(names)
        records.append(record)
    return tuple(records)


_SCP_LIKE = re.compile(r"^(?:[^@/]+@)?([^:]+):(.+)$")


def normalize_remote_url(url: str) -> str:
    """Normalize HTTPS/SSH GitHub remotes to lower-case ``owner/name``."""

    value = url.strip().removesuffix(".git").rstrip("/")
    if "://" in value:
        path = value.split("://", 1)[1].split("/", 1)
        if len(path) != 2:
            raise RepositoryIdentityError(f"unsupported remote URL: {url}")
        host = path[0].rsplit("@", 1)[-1].split(":", 1)[0]
        repo_path = path[1]
    else:
        match = _SCP_LIKE.match(value)
        if match is None:
            raise RepositoryIdentityError(f"unsupported remote URL: {url}")
        host, repo_path = match.groups()
    if host.casefold() != "github.com" or repo_path.count("/") != 1:
        raise RepositoryIdentityError(f"remote is not a GitHub owner/repository: {url}")
    return repo_path.casefold()


def _load_sentinel(repo_root: Path) -> dict[str, Any]:
    path = repo_root / _SENTINEL
    try:
        sentinel = _object(json.loads(path.read_text(encoding="utf-8")), label="sentinel")
    except (OSError, json.JSONDecodeError) as exc:
        raise RepositoryIdentityError(f"missing or invalid repository sentinel: {exc}") from exc
    if sentinel.get("schema_version") != 1:
        raise RepositoryIdentityError("unsupported repository sentinel schema")
    return sentinel


def verify_repository_identity(
    *,
    repo_path: str,
    resolved_repository_id: int,
    objective_role: RepositoryRole,
    archival_override_reason: ArchivalOverrideReason | None = None,
    manifest_path: Path = _MANIFEST,
) -> VerifiedRepositoryIdentity:
    """Verify identity and role before worktree creation or mutation.

    ``resolved_repository_id`` must come from GitHub/API resolution, so an old
    redirect and the renamed repository map to the same dormant record.
    """

    picked = Path(repo_path)
    if not picked.is_dir():
        raise RepositoryIdentityError(f"repository path is not a directory: {repo_path}")
    root = Path(_git(picked, "rev-parse", "--show-toplevel"))
    records = load_manifest(manifest_path)
    record = next((r for r in records if r.repository_id == resolved_repository_id), None)
    if record is None:
        raise RepositoryIdentityError(f"repository ID {resolved_repository_id} is not canonical")

    fetch_url = _git(root, "remote", "get-url", "origin")
    push_url = _git(root, "remote", "get-url", "--push", "origin")
    allowed_names = {record.full_name.casefold(), *(a.casefold() for a in record.aliases)}
    for label, url in (("fetch", fetch_url), ("push", push_url)):
        if normalize_remote_url(url) not in allowed_names:
            raise RepositoryIdentityError(
                f"{label} remote conflicts with repository ID {record.repository_id}: {url}"
            )

    default_ref = _git(root, "symbolic-ref", "refs/remotes/origin/HEAD")
    default_branch = default_ref.removeprefix("refs/remotes/origin/")
    if default_branch != record.default_branch:
        raise RepositoryIdentityError(
            f"default branch mismatch: expected {record.default_branch}, got {default_branch}"
        )
    head_sha = _git(root, "rev-parse", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40}", head_sha):
        raise RepositoryIdentityError("resolved HEAD is not a full commit SHA")

    sentinel = _load_sentinel(root)
    expected = {
        "repository_id": record.repository_id,
        "owner": record.owner,
        "name": record.name,
        "role": record.role.value,
        "state": record.state,
    }
    actual = {key: sentinel.get(key) for key in expected}
    if actual != expected:
        raise RepositoryIdentityError(f"repository sentinel mismatch: {actual!r}")

    if record.role is RepositoryRole.DORMANT_STANDALONE_ARCHIVE:
        if objective_role is not record.role or archival_override_reason is None:
            raise RepositoryIdentityError(
                "dormant standalone repository is forbidden without a typed archival override"
            )
    elif archival_override_reason is not None:
        raise RepositoryIdentityError("archival override is valid only for a dormant repository")
    elif record.role is not objective_role:
        raise RepositoryIdentityError(
            f"objective role {objective_role.value} conflicts with repository role "
            f"{record.role.value}"
        )

    return VerifiedRepositoryIdentity(
        repository_id=record.repository_id,
        full_name=record.full_name,
        role=record.role,
        state=record.state,
        fetch_url=fetch_url,
        push_url=push_url,
        default_branch=default_branch,
        head_sha=head_sha,
        archival_override_reason=archival_override_reason,
    )
