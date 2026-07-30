"""Validation tests (issue #38 §3).

These tests cover every validation rule: SHA format, target
existence, lineage ancestry, expected-current SHA matching the
live metadata, and the failure-mode error mapping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.updater.protocol import (
    Authorization,
    RequestRecord,
    new_request_id,
)
from omnigent.updater.validation import (
    LineageRejectedError,
    MalformedShaError,
    StaleExpectedCurrentError,
    TargetMissingError,
    is_descendant_of,
    read_live_metadata,
    target_exists_on_fork,
    validate_expected_current,
    validate_request,
    validate_sha_format,
    validate_target,
)


def _auth() -> Authorization:
    return Authorization(kind="operator", operator="tester")


def _record(target: str, expected: str) -> RequestRecord:
    return RequestRecord(
        request_id=new_request_id(),
        target_sha=target,
        expected_current_sha=expected,
        origin_session_id=None,
        origin_conversation_id=None,
        requested_by="operator:tester",
        created_at="2026-01-01T00:00:00Z",
        authorization=_auth(),
    )


def test_validate_sha_format_rejects_uppercase() -> None:
    with pytest.raises(MalformedShaError):
        validate_sha_format(
            target_sha="0123456789ABCDEF0123456789ABCDEF01234567",
            expected_current_sha="0" * 40,
        )


def test_validate_sha_format_rejects_short_sha() -> None:
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="abc", expected_current_sha="0" * 40)


def test_validate_sha_format_accepts_lowercase_hex() -> None:
    validate_sha_format(target_sha="0" * 40, expected_current_sha="0" * 40)


def test_target_exists_on_fork_recognizes_real_commit(repo_root: Path, make_commit) -> None:
    """A commit that exists in the repo is recognized."""
    sha = make_commit("hello")
    assert target_exists_on_fork(repo_root, sha) is True


def test_target_exists_on_fork_rejects_unknown_sha(repo_root: Path) -> None:
    """An unknown SHA is rejected before any lineage check."""
    assert target_exists_on_fork(repo_root, "0" * 40) is False


def test_is_descendant_of_handles_linear_history(repo_root: Path, make_commit) -> None:
    """A child commit is a descendant of its parent."""
    parent = make_commit("parent")
    child = make_commit("child")
    assert is_descendant_of(repo_root, ancestor=parent, descendant=child) is True
    assert is_descendant_of(repo_root, ancestor=child, descendant=parent) is False


def test_validate_target_rejects_out_of_lineage(
    repo_root: Path, make_commit, lineage_anchor: str
) -> None:
    """A non-descendant of the lineage anchor is rejected."""
    # The lineage anchor was already committed before the test
    # ran. Create a branch the anchor is not an ancestor of by
    # using a separate repo entirely.
    import subprocess

    other = repo_root.parent / "other_lineage_test"
    other.mkdir()
    subprocess.run(
        ["git", "-C", str(other), "init", "--initial-branch=main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.name", "Test"],
        check=True,
    )
    (other / "X").write_text("x")
    subprocess.run(["git", "-C", str(other), "add", "X"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "out of lineage", "-q"], check=True)
    orphan_sha = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Make the orphan reachable on the fork via a remote.
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "other_lineage_test", str(other)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "other_lineage_test", "-q"],
        check=True,
    )
    with pytest.raises(LineageRejectedError):
        validate_target(repo_root, orphan_sha)


def test_validate_target_rejects_unknown_sha(repo_root: Path, lineage_anchor: str) -> None:
    """A non-existent SHA is rejected with ``TargetMissingError``."""
    with pytest.raises(TargetMissingError):
        validate_target(repo_root, "0" * 40)


def test_validate_target_accepts_descendant(
    repo_root: Path, make_commit, lineage_anchor: str
) -> None:
    """A commit on the main branch (a descendant of the anchor) is accepted."""
    sha = make_commit("descendant")
    validate_target(repo_root, sha)


def test_read_live_metadata_returns_empty_when_missing(
    live_sha_file: Path,
) -> None:
    """An empty live-SHA file yields ``live_sha == ''``."""
    meta = read_live_metadata(live_sha_file)
    assert meta.live_sha == ""
    assert meta.source_path == live_sha_file


def test_read_live_metadata_reads_existing_sha(live_sha_file: Path) -> None:
    live_sha_file.write_text("0123456789abcdef0123456789abcdef01234567\n")
    meta = read_live_metadata(live_sha_file)
    assert meta.live_sha == "0123456789abcdef0123456789abcdef01234567"


def test_validate_expected_current_rejects_stale_sha(
    live_sha_file: Path,
) -> None:
    live_sha_file.write_text("0123456789abcdef0123456789abcdef01234567\n")
    record = _record(target="0" * 40, expected="0000000000000000000000000000000000000000")
    with pytest.raises(StaleExpectedCurrentError):
        validate_expected_current(record, live_sha="0123456789abcdef0123456789abcdef01234567")


def test_validate_expected_current_accepts_matching_sha(
    live_sha_file: Path,
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    record = _record(target=sha, expected=sha)
    validate_expected_current(record, live_sha=sha)


def test_validate_expected_current_requires_empty_marker_for_first_install(
    live_sha_file: Path,
) -> None:
    """A first-time install expects ``expected_current_sha == '0' * 40``."""
    record = _record(
        target="0123456789abcdef0123456789abcdef01234567",
        expected="0000000000000000000000000000000000000001",
    )
    with pytest.raises(StaleExpectedCurrentError):
        validate_expected_current(record, live_sha="")


def test_validate_request_combines_all_checks(
    repo_root: Path, make_commit, lineage_anchor: str, live_sha_file: Path
) -> None:
    """The top-level ``validate_request`` wires every check."""
    sha = make_commit("descendant")
    live = "0" * 40
    record = _record(target=sha, expected=live)
    validate_request(record, repo=repo_root, live_sha=live)


def test_validate_request_rejects_malformed_sha(
    repo_root: Path, lineage_anchor: str, live_sha_file: Path
) -> None:
    """``validate_request`` raises ``MalformedShaError`` for malformed SHAs.

    Exercises the lower-level :func:`validate_sha_format` helper
    that :func:`validate_request` calls, since the
    :class:`RequestRecord` constructor itself enforces the same
    rule at construction time.
    """
    with pytest.raises(MalformedShaError):
        validate_sha_format(
            target_sha="not-a-sha",
            expected_current_sha="0" * 40,
        )


def test_validate_request_rejects_stale(
    repo_root: Path, make_commit, lineage_anchor: str, live_sha_file: Path
) -> None:
    """A request whose expected SHA does not match the live SHA is stale."""
    target = make_commit("descendant")
    record = _record(target=target, expected="0" * 40)
    live = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(StaleExpectedCurrentError):
        validate_request(record, repo=repo_root, live_sha=live)


# ---------------------------------------------------------------------------
# Phase 2 hardening — explicit fork/main ancestry + remote URL + symbol/version
# rejection (issue #38 §3).
# ---------------------------------------------------------------------------


def test_validate_sha_format_rejects_symbolic_ref() -> None:
    """A symbolic ref like ``fork/main`` is not a 40-char SHA."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="fork/main", expected_current_sha="0" * 40)


def test_validate_sha_format_rejects_branch_name() -> None:
    """An arbitrary branch name is not a 40-char SHA."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(
            target_sha="feat/issue-38-external-self-update-controller",
            expected_current_sha="0" * 40,
        )


def test_validate_sha_format_rejects_version_string() -> None:
    """A SemVer string like ``v0.7.0`` is rejected at format validation."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="v0.7.0", expected_current_sha="0" * 40)


def test_validate_sha_format_rejects_latest() -> None:
    """The literal string ``latest`` is rejected at format validation."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="latest", expected_current_sha="0" * 40)


def test_validate_sha_format_rejects_empty_sha() -> None:
    """An empty string is rejected at format validation."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="", expected_current_sha="0" * 40)


def test_validate_sha_format_rejects_partial_sha() -> None:
    """A 7-character short SHA is rejected at format validation.

    Git allows abbreviated SHAs but the updater never accepts
    them — the request must contain the exact merged SHA so the
    audit trail is unambiguous.
    """
    with pytest.raises(MalformedShaError):
        validate_sha_format(target_sha="0123456", expected_current_sha="0" * 40)


def test_validate_sha_format_rejects_uppercase_hex() -> None:
    """Uppercase hex characters are rejected (spec requires lowercase)."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(
            target_sha="0123456789ABCDEF0123456789ABCDEF01234567",
            expected_current_sha="0" * 40,
        )


def test_validate_sha_format_rejects_non_hex() -> None:
    """A 40-character string with non-hex characters is rejected."""
    with pytest.raises(MalformedShaError):
        validate_sha_format(
            target_sha="z" * 40,
            expected_current_sha="0" * 40,
        )


def test_fork_remote_url_rejects_unapproved_remote(
    repo_root: Path, lineage_anchor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``fork`` remote pointing at the wrong repo is rejected loudly.

    The conftest registers the local mirror as an approved URL so
    other tests pass; we strip that override here so only the
    production allow-list applies.
    """
    from omnigent.updater.validation import (
        ForkRemoteUrlError,
        verify_fork_remote_url,
    )

    monkeypatch.delenv("OMNIGENT_UPDATER_APPROVED_FORK_URLS", raising=False)
    # Re-point the `fork` remote at an unrelated repository.
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "remote",
            "set-url",
            "fork",
            "https://github.com/attacker/evil-fork.git",
        ],
        check=True,
        capture_output=True,
    )
    with pytest.raises(ForkRemoteUrlError):
        verify_fork_remote_url(repo_root)


def test_fork_remote_url_accepts_approved_https(
    repo_root: Path, lineage_anchor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The approved HTTPS URL is accepted when no test override is active."""
    from omnigent.updater.validation import verify_fork_remote_url

    monkeypatch.delenv("OMNIGENT_UPDATER_APPROVED_FORK_URLS", raising=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "remote",
            "set-url",
            "fork",
            "https://github.com/Mortified2896/omnigent.git",
        ],
        check=True,
        capture_output=True,
    )
    verify_fork_remote_url(repo_root)


def test_fork_remote_url_accepts_approved_ssh(
    repo_root: Path, lineage_anchor: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The SSH form ``git@github.com:Mortified2896/omnigent.git`` is accepted."""
    from omnigent.updater.validation import verify_fork_remote_url

    monkeypatch.delenv("OMNIGENT_UPDATER_APPROVED_FORK_URLS", raising=False)
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "remote",
            "set-url",
            "fork",
            "git@github.com:Mortified2896/omnigent.git",
        ],
        check=True,
        capture_output=True,
    )
    verify_fork_remote_url(repo_root)


def test_fork_main_ref_resolved_requires_fetched_ref(repo_root: Path, lineage_anchor: str) -> None:
    """``refs/remotes/fork/main`` is present after the conftest fetches it."""
    from omnigent.updater.validation import fork_main_ref_resolved

    assert fork_main_ref_resolved(repo_root) is True


def test_target_is_commit_on_fork_rejects_tag(repo_root: Path, make_commit) -> None:
    """An annotated tag's tag-object SHA is rejected.

    A lightweight tag's SHA is the commit SHA, so we use an
    annotated tag (``git tag -a``) to exercise the tag-object
    rejection path.
    """
    from omnigent.updater.validation import target_is_commit_on_fork

    commit_sha = make_commit("tagged")
    subprocess.run(
        [
            "git",
            "-C",
            str(repo_root),
            "tag",
            "-a",
            "-m",
            "annotated test tag",
            "v9.9.9-test",
            commit_sha,
        ],
        check=True,
        capture_output=True,
    )
    # `git rev-parse <tag>` returns the tag-object SHA (not the
    # commit it points at) for annotated tags.
    tag_obj_sha = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "v9.9.9-test^{tag}"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    assert target_is_commit_on_fork(repo_root, commit_sha) is True
    assert target_is_commit_on_fork(repo_root, tag_obj_sha) is False


def test_validate_target_rejects_unmerged_feature_branch_commit(
    repo_root: Path, lineage_anchor: str
) -> None:
    """A commit on a feature branch not merged into ``refs/remotes/fork/main``
    is rejected with ``LineageRejectedError`` even though it resolves to
    a valid commit object on the local repo."""
    # Create a separate orphan repo and copy a single commit from it
    # as a remote, the way the upstream-only test does.
    other = repo_root.parent / "feature_branch_test"
    other.mkdir()
    subprocess.run(
        ["git", "-C", str(other), "init", "--initial-branch=main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.name", "Test"],
        check=True,
    )
    (other / "F").write_text("f")
    subprocess.run(["git", "-C", str(other), "add", "F"], check=True)
    subprocess.run(
        ["git", "-C", str(other), "commit", "-m", "feature branch tip", "-q"], check=True
    )
    branch_sha = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Attach as a remote under a name that is not `fork`.
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "feature_branch_test", str(other)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "feature_branch_test", "-q"],
        check=True,
    )
    with pytest.raises(LineageRejectedError):
        validate_target(repo_root, branch_sha)


def test_validate_request_rejects_unmerged_branch_target(
    repo_root: Path, lineage_anchor: str, live_sha_file: Path
) -> None:
    """``validate_request`` rejects a target SHA that is on a feature branch
    but not an ancestor of ``refs/remotes/fork/main``."""
    other = repo_root.parent / "feature_branch_request_test"
    other.mkdir()
    subprocess.run(
        ["git", "-C", str(other), "init", "--initial-branch=main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other), "config", "user.name", "Test"],
        check=True,
    )
    (other / "F").write_text("f")
    subprocess.run(["git", "-C", str(other), "add", "F"], check=True)
    subprocess.run(["git", "-C", str(other), "commit", "-m", "tip", "-q"], check=True)
    branch_sha = subprocess.run(
        ["git", "-C", str(other), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "feature_branch_req", str(other)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "feature_branch_req", "-q"],
        check=True,
    )
    record = _record(target=branch_sha, expected="0" * 40)
    with pytest.raises(LineageRejectedError):
        validate_request(record, repo=repo_root, live_sha="0" * 40)
