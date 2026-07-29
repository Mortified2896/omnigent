"""Commit SHA resolution and validation for the promotion pipeline.

The promotion script accepts any of:

* a full 40-character SHA,
* a short 7+ character SHA,
* a branch/tag name (resolved via ``git rev-parse`` against ``fork/main``
  by default or the configured ``OMNIGENT_PROMOTE_FROM_REMOTE``),
* the literal string ``HEAD`` (resolved against the promotion source).

This module normalizes all of those inputs into the canonical full SHA
the rest of the promotion pipeline uses. The functions are pure (no
filesystem mutations beyond running ``git`` as a subprocess) and
deterministic — so they can be unit-tested in isolation and the tests
double as documentation of the expected behavior for the LLM agents
that drive ``scripts/promote_release.sh``.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

_FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
_SHORT_SHA = re.compile(r"^[0-9a-f]{7,39}$")
_REF_NAME = re.compile(r"^[0-9A-Za-z._/-]+$")


class ReleaseIdError(RuntimeError):
    """Raised when the requested ref/SHA cannot be resolved."""

    def __init__(self, message: str, *, requested: str) -> None:
        super().__init__(message)
        self.requested = requested


def _run_git(repo: Path, args: list[str]) -> str:
    """Run ``git`` against ``repo`` and return stdout (stripped).

    Raises :class:`ReleaseIdError` on a non-zero exit code with the
    stderr captured; the promotion script surfaces this verbatim in
    the operator's terminal.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError as exc:
        raise ReleaseIdError(
            "git executable not found; required to resolve release SHAs",
            requested="",
        ) from exc
    if proc.returncode != 0:
        raise ReleaseIdError(
            f"git {' '.join(args)} failed (rc={proc.returncode}): {proc.stderr.strip()}",
            requested=" ".join(args),
        )
    return proc.stdout.strip()


def normalize_ref(repo: Path, requested: str, *, default_remote: str = "fork") -> str:
    """Resolve ``requested`` to a full 40-character SHA.

    :param repo: Path to a git working tree (the main checkout; the
        release directory's ``.git`` is not what we want — we'd have
        to fetch first to make it useful for refs).
    :param requested: The string the operator (or agent) typed. Empty
        string is treated as the configured ``default_remote``/``main``.
    :param default_remote: The remote to resolve from when ``requested``
        is empty. Defaults to ``fork`` to match the rest of the repo's
        workflows (the user's fork under ``Mortified2896``).
    :returns: The full 40-character lowercase SHA.
    :raises ReleaseIdError: When ``requested`` is not a SHA, short SHA,
        or ref-like name; or when ``git rev-parse`` fails.
    """
    if not repo.is_dir():
        raise ReleaseIdError(
            f"repo path does not exist: {repo}",
            requested=requested,
        )

    requested = (requested or "").strip()
    if not requested:
        requested = f"{default_remote}/main"

    if _FULL_SHA.match(requested):
        return requested
    if _SHORT_SHA.match(requested):
        # Use ``git rev-parse`` to expand the short SHA to a full
        # SHA; this catches typos and catches "this short SHA matches
        # two commits" ambiguity.
        try:
            return _run_git(repo, ["rev-parse", requested])
        except ReleaseIdError as exc:
            raise ReleaseIdError(
                f"could not resolve short SHA {requested!r}: {exc}",
                requested=requested,
            ) from exc
    if not _REF_NAME.match(requested):
        raise ReleaseIdError(
            f"ref {requested!r} is not a valid SHA or ref name",
            requested=requested,
        )
    return _run_git(repo, ["rev-parse", requested])


def fetch(repo: Path, *, remote: str | None = None) -> None:
    """Best-effort ``git fetch`` of the configured remote.

    The promotion script calls this before resolving refs so a freshly
    pushed branch on ``fork`` is visible. The fetch is best-effort —
    failure to fetch (e.g. offline mode, no remote configured) is a
    soft error: ``normalize_ref`` will still succeed against the
    locally available refs.
    """
    remote_name = (
        remote or os.environ.get("OMNIGENT_PROMOTE_FROM_REMOTE", "fork")
    ).strip() or "fork"
    try:
        subprocess.run(
            ["git", "-C", str(repo), "fetch", remote_name],
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        pass


def main() -> int:
    """CLI helper for ad-hoc SHA resolution.

    ``python -m omnigent.deploy.ops.release_id <repo> <ref>`` prints
    the full SHA on stdout, or exits non-zero with the error on
    stderr. Used by the ``scripts/release_id.sh`` helper so an agent
    can ask ``what would promote-release resolve this to?`` without
    actually promoting.
    """
    import sys

    args = list(sys.argv[1:])
    if len(args) != 2:
        print(f"usage: {sys.argv[0]} <repo> <ref>", file=sys.stderr)
        return 2
    try:
        sha = normalize_ref(Path(args[0]), args[1])
    except ReleaseIdError as exc:
        print(f"[release-id] ERROR: {exc}", file=sys.stderr)
        return 1
    print(sha)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
