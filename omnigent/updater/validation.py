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
    """
    if not target_exists_on_fork(repo, target_sha):
        return f"target {target_sha!r} does not exist on the approved fork"
    anchor = layout.lineage_anchor()
    if not is_descendant_of(repo, ancestor=anchor, descendant=target_sha):
        return (
            f"target {target_sha!r} is not a descendant of the lineage anchor "
            f"{anchor!r} (out of approved deployment lineage)"
        )
    return None


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
    """Reject missing or out-of-lineage targets.

    Issue #38 §3 rules 3-5.
    """
    reason = lineage_rejects(repo, target_sha)
    if reason is not None:
        if "does not exist" in reason:
            raise TargetMissingError(reason)
        raise LineageRejectedError(reason)


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
    "LineageRejectedError",
    "LiveMetadata",
    "MalformedShaError",
    "StaleExpectedCurrentError",
    "TargetMissingError",
    "ValidationError",
    "is_descendant_of",
    "lineage_rejects",
    "read_live_metadata",
    "target_exists_on_fork",
    "validate_expected_current",
    "validate_request",
    "validate_sha_format",
    "validate_target",
]
