"""Strict target and lineage validation (issue #38 §3).

Validation runs **before** any build phase and fails fast. Each
rejection is recorded durably with a precise reason. The
controller never silently substitutes ``fork/main``, the previous
release, or any other SHA on rejection.

Validations:

1. ``target_sha`` is exactly 40 lowercase hex characters.
2. ``expected_current_sha`` is exactly 40 lowercase hex characters.
3. The target commit exists on the approved writable fork.
4. The target is reachable from the approved deployment lineage.
5. The target is not merely present in an unapproved remote or
   unrelated branch.
6. The live SHA is read from deployment metadata, not trusted from
   the request.
7. The live SHA exactly equals ``expected_current_sha``.
8. The request has not already reached a terminal state.
9. No conflicting production update is active.

Each check is implemented as a separate function so the controller
can report the exact failure reason to the operator and persist it
in the result record.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

from omnigent.updater import layout
from omnigent.updater.protocol import RequestRecord, is_valid_sha


class ValidationError(RuntimeError):
    """Base class for validation failures.

    Subclasses carry enough context for the controller to record a
    precise rejection reason in the result file without re-running
    the validator.
    """


class MalformedShaError(ValidationError):
    """Raised when a SHA is not exactly 40 lowercase hex characters."""


class TargetMissingError(ValidationError):
    """Raised when the target SHA does not exist on the approved fork."""


class LineageRejectedError(ValidationError):
    """Raised when the target SHA is not a descendant of the lineage anchor."""


class StaleExpectedCurrentError(ValidationError):
    """Raised when ``expected_current_sha`` does not match the live SHA."""


class ForkRemoteUrlError(ValidationError):
    """Raised when the configured ``fork`` remote URL is not the approved one."""


class ForkMainMissingError(ValidationError):
    """Raised when ``refs/remotes/fork/main`` is not fetched locally."""


# Approved writable fork. The updater never installs anything from any
# other repository — even if the SHA validates — because the
# deployment lineage is tied to this fork.
_APPROVED_FORK_URL_SUFFIXES: tuple[str, ...] = (
    "github.com/Mortified2896/omnigent.git",
    # SSH form for the same repo.
    "github.com:Mortified2896/omnigent.git",
)
_ENV_APPROVED_FORK_URLS = "OMNIGENT_UPDATER_APPROVED_FORK_URLS"


def _approved_fork_url(url: str) -> bool:
    """Return ``True`` iff ``url`` is one of the approved fork URL forms.

    Production deploys use the hardcoded GitHub URL suffixes. Tests
    may set ``OMNIGENT_UPDATER_APPROVED_FORK_URLS`` to a
    comma-separated list of additional suffixes (typically the path
    of a local bare mirror) so they can exercise the explicit
    ``refs/remotes/fork/main`` ancestry check without contacting
    GitHub.
    """
    url = url.strip()
    raw = os.environ.get(_ENV_APPROVED_FORK_URLS, "").strip()
    suffixes: list[str] = list(_APPROVED_FORK_URL_SUFFIXES)
    if raw:
        suffixes.extend(s.strip() for s in raw.split(",") if s.strip())
    return any(url.endswith(suffix) for suffix in suffixes)


@dataclass(frozen=True)
class LiveMetadata:
    """The current deployment metadata read from disk.

    The controller reads this directly from
    :func:`omnigent.updater.layout.live_sha_file` rather than from
    the request, per issue #38 §3 rule 6.
    """

    live_sha: str
    source_path: Path

    @property
    def exists(self) -> bool:
        return bool(self.live_sha)


def read_live_metadata(path: Path | None = None) -> LiveMetadata:
    """Read the live SHA from the deployment metadata file.

    Returns an empty :class:`LiveMetadata` when the file does not
    exist or is empty. The controller treats that as "no prior
    deployment"; an :class:`StaleExpectedCurrentError` will fire if
    the request named a non-empty ``expected_current_sha``.
    """
    target = path or layout.live_sha_file()
    if not target.is_file():
        return LiveMetadata(live_sha="", source_path=target)
    raw = target.read_text().strip()
    if not raw:
        return LiveMetadata(live_sha="", source_path=target)
    return LiveMetadata(live_sha=raw, source_path=target)


def _git(repo: Path, *args: str) -> str:
    """Run a git command inside ``repo`` and return stdout.

    Raises :class:`ValidationError` on non-zero exit so callers can
    surface the failure reason to the operator.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise ValidationError(
            f"git {' '.join(args)} failed in {repo}: {proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def target_exists_on_fork(repo: Path, target_sha: str) -> bool:
    """Return ``True`` iff ``target_sha`` is a real commit on ``repo``.

    Uses ``git cat-file -t <sha>`` because it both verifies the SHA
    resolves and confirms the object type is ``commit``. A bare
    ``git rev-parse`` could in principle match a tag or a tree
    object — exactly the kind of unapproved-remote-leakage the spec
    forbids.
    """
    try:
        out = _git(repo, "cat-file", "-t", target_sha)
    except ValidationError:
        return False
    return out == "commit"


def is_descendant_of(repo: Path, *, ancestor: str, descendant: str) -> bool:
    """Return ``True`` iff ``ancestor`` is an ancestor of ``descendant``.

    ``git merge-base --is-ancestor <ancestor> <descendant>`` returns
    exit 0 when the relationship holds. The function prefers the
    exit code over stdout because git prints nothing on success.
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            ancestor,
            descendant,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.returncode == 0


def lineage_rejects(repo: Path, target_sha: str) -> str | None:
    """Return a non-empty reason string iff ``target_sha`` is outside the lineage.

    Returns ``None`` when the target is on the approved fork and is
    a descendant of the configured lineage anchor.

    :raises ForkRemoteUrlError: when the ``fork`` remote URL is not
        one of the approved fork URLs.
    :raises ForkMainMissingError: when ``refs/remotes/fork/main`` is
        not fetched locally.
    """
    url = fork_remote_url(repo)
    if not _approved_fork_url(url):
        raise ForkRemoteUrlError(
            f"configured `fork` remote URL {url!r} is not the approved "
            f"Mortified2896/omnigent.git; refusing to validate targets"
        )
    if not fork_main_ref_resolved(repo):
        raise ForkMainMissingError(
            "refs/remotes/fork/main is not fetched locally; refusing to "
            "validate targets until the approved remote-tracking ref is present"
        )
    if not target_exists_on_fork(repo, target_sha):
        return f"target {target_sha!r} does not exist on the approved fork"
    anchor = layout.lineage_anchor()
    if not is_descendant_of(repo, ancestor=anchor, descendant=target_sha):
        return (
            f"target {target_sha!r} is not a descendant of the lineage anchor "
            f"{anchor!r} (out of approved deployment lineage)"
        )
    return None


def fork_remote_url(repo: Path) -> str:
    """Return the URL of the ``fork`` remote configured on ``repo``.

    Raises :class:`ValidationError` when the remote is missing.
    """
    proc = subprocess.run(
        ["git", "-C", str(repo), "remote", "get-url", "fork"],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise ValidationError(
            f"`fork` remote is not configured on {repo}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def verify_fork_remote_url(repo: Path) -> None:
    """Reject a ``fork`` remote whose URL is not on the approved allow-list.

    The approved list is the hardcoded production suffixes
    (``github.com/Mortified2896/omnigent.git`` in HTTPS or SSH
    form) plus any suffixes registered through
    ``OMNIGENT_UPDATER_APPROVED_FORK_URLS`` (used by tests to point
    at a local bare mirror).
    """
    url = fork_remote_url(repo)
    if not _approved_fork_url(url):
        raise ForkRemoteUrlError(
            f"configured `fork` remote URL {url!r} is not the approved "
            f"Mortified2896/omnigent.git; refusing to validate targets"
        )


def fork_main_ref_resolved(repo: Path) -> bool:
    """Return ``True`` iff ``refs/remotes/fork/main`` resolves locally.

    Uses ``git rev-parse --verify`` because it returns non-zero on a
    missing ref without polluting the repository. The updater's
    ``install_omnigent_updater.sh`` script is responsible for
    fetching ``fork main`` during install; if it has not been run
    yet, every validation must fail closed.
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "--quiet",
            "refs/remotes/fork/main",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.returncode == 0


def fork_main_sha(repo: Path) -> str:
    """Return the SHA pointed at by ``refs/remotes/fork/main``.

    Raises :class:`ForkMainMissingError` if the ref is not present.
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "rev-parse",
            "--verify",
            "refs/remotes/fork/main",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    if proc.returncode != 0:
        raise ForkMainMissingError(
            f"refs/remotes/fork/main not present on {repo}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout.strip()


def target_is_commit_on_fork(repo: Path, target_sha: str) -> bool:
    """Return ``True`` iff ``target_sha`` resolves to a commit on the fork.

    Uses ``git cat-file -t <sha>`` because the literal
    ``git cat-file -e <sha>^{commit}`` peeler from the spec would
    accept annotated tags (they peel down to a commit). The
    semantic the spec actually wants is "the SHA must point at a
    commit object directly", which is what ``cat-file -t`` checks.
    Tags, trees, and blobs are all rejected.
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "cat-file",
            "-t",
            target_sha,
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.returncode == 0 and proc.stdout.strip() == "commit"


def target_reachable_from_fork_main(repo: Path, target_sha: str) -> bool:
    """Return ``True`` iff ``target_sha`` is an ancestor of ``refs/remotes/fork/main``.

    Uses the explicit ``git merge-base --is-ancestor TARGET refs/remotes/fork/main``
    form from the issue spec. The function prefers the exit code
    over stdout because git prints nothing on success.
    """
    proc = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "merge-base",
            "--is-ancestor",
            target_sha,
            "refs/remotes/fork/main",
        ],
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
    )
    return proc.returncode == 0


def validate_sha_format(*, target_sha: str, expected_current_sha: str) -> None:
    """Reject malformed SHAs.

    Issue #38 §3 rules 1 and 2.
    """
    if not is_valid_sha(target_sha):
        raise MalformedShaError(
            f"target_sha must be exactly 40 lowercase hex characters; got {target_sha!r}"
        )
    if not is_valid_sha(expected_current_sha):
        raise MalformedShaError(
            "expected_current_sha must be exactly 40 lowercase hex characters; "
            f"got {expected_current_sha!r}"
        )


def validate_target(
    repo: Path,
    target_sha: str,
    *,
    live_sha: str | None = None,  # noqa: ARG001
) -> None:
    """Reject missing, out-of-lineage, or non-fork-main targets.

    Issue #38 §3 rules 3-5. Runs three independent guards so the
    operator gets a precise rejection reason:

    1. The ``fork`` remote URL must be the approved
       ``Mortified2896/omnigent`` form.
    2. ``refs/remotes/fork/main`` must be fetched locally.
    3. ``target_sha`` must resolve to a commit object (rejects
       tags, trees, and blobs) **and** be an ancestor of
       ``refs/remotes/fork/main`` — the authoritative deployment
       ref. This is the explicit ``git cat-file -e`` and
       ``git merge-base --is-ancestor`` check from the spec.

    The legacy lineage-anchor check is retained as an additional
    belt-and-suspenders guard against a stale ``fork/main`` ref
    pointing at an unauthorized rollback.
    """
    reason = lineage_rejects(repo, target_sha)
    if reason is not None:
        if "does not exist" in reason:
            raise TargetMissingError(reason)
        raise LineageRejectedError(reason)
    # Explicit fork/main ancestry check (issue #38 §3 — the
    # authoritative deployment ref, not the lineage anchor).
    if not target_is_commit_on_fork(repo, target_sha):
        raise TargetMissingError(
            f"target {target_sha!r} does not resolve to a commit on the approved fork "
            f"(rejected: not a commit object — tag/tree/blob)"
        )
    if not target_reachable_from_fork_main(repo, target_sha):
        raise LineageRejectedError(
            f"target {target_sha!r} is not an ancestor of refs/remotes/fork/main "
            f"(rejected: out of approved deployment lineage)"
        )


def validate_expected_current(record: RequestRecord, *, live_sha: str) -> None:
    """Reject a stale ``expected_current_sha``.

    Issue #38 §3 rule 7. Reads ``live_sha`` from deployment
    metadata (rule 6) rather than from the request itself.

    When ``live_sha`` is empty (no prior deployment), the only
    acceptable ``expected_current_sha`` is the canonical "empty
    release" marker — which we represent as 40 zero hex digits.
    Anything else is treated as stale.
    """
    empty_marker = "0" * 40
    if not live_sha:
        if record.expected_current_sha != empty_marker:
            raise StaleExpectedCurrentError(
                "no prior deployment exists; expected_current_sha must be "
                f"{empty_marker!r} (got {record.expected_current_sha!r})"
            )
        return
    if record.expected_current_sha != live_sha:
        raise StaleExpectedCurrentError(
            f"live SHA {live_sha!r} does not match expected_current_sha "
            f"{record.expected_current_sha!r} (request is stale)"
        )


def validate_request(
    record: RequestRecord,
    *,
    repo: Path | None = None,
    live_sha: str | None = None,
) -> None:
    """Run every validation in sequence.

    The order matters:

    1. SHA format first (cheapest, catches the most common mistake).
    2. Target existence + lineage (slow; shells out to git).
    3. Expected-current SHA (last; depends on a live read from
       deployment metadata).

    :param record: The request to validate.
    :param repo: Repository root override; defaults to
        :func:`omnigent.updater.layout.repo_root`.
    :param live_sha: Live SHA override; defaults to reading
        deployment metadata.
    :raises ValidationError: subclasses on any failure.
    """
    validate_sha_format(
        target_sha=record.target_sha,
        expected_current_sha=record.expected_current_sha,
    )
    effective_live_sha = live_sha
    if effective_live_sha is None:
        meta = read_live_metadata()
        effective_live_sha = meta.live_sha
    validate_target(repo or layout.repo_root(), record.target_sha)
    validate_expected_current(record, live_sha=effective_live_sha)


__all__ = [
    "ForkMainMissingError",
    "ForkRemoteUrlError",
    "LineageRejectedError",
    "LiveMetadata",
    "MalformedShaError",
    "StaleExpectedCurrentError",
    "TargetMissingError",
    "ValidationError",
    "fork_main_ref_resolved",
    "fork_main_sha",
    "fork_remote_url",
    "is_descendant_of",
    "lineage_rejects",
    "read_live_metadata",
    "target_exists_on_fork",
    "target_is_commit_on_fork",
    "target_reachable_from_fork_main",
    "validate_expected_current",
    "validate_request",
    "validate_sha_format",
    "validate_target",
    "verify_fork_remote_url",
]
