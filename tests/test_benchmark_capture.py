from __future__ import annotations

import subprocess
import tarfile
from pathlib import Path

import pytest

from omnigent.benchmark_capture import BenchmarkCaptureError, snapshot_git_state


def _git(repo: Path, *args: str, input_bytes: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return result.stdout


def _init_repo(path: Path) -> str:
    path.mkdir()
    _git(path, "init")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Benchmark Capture Test")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    (path / "staged.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "base")
    return _git(path, "rev-parse", "HEAD").decode().strip()


def test_snapshot_reconstructs_dirty_workspace(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    head = _init_repo(repo)

    (repo / "tracked.txt").write_text("base\nunstaged\n", encoding="utf-8")
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git(repo, "add", "staged.txt")

    nested = repo / "nested"
    nested.mkdir()
    (nested / "untracked.bin").write_bytes(b"\x00\x01benchmark")
    (repo / "untracked-link").symlink_to("tracked.txt")

    capture = tmp_path / "capture"
    metadata = snapshot_git_state(repo, capture)

    assert metadata["head"] == head
    assert metadata["detached"] is False
    assert metadata["untracked_paths"] == ["nested/untracked.bin", "untracked-link"]
    assert (capture / "git.json").is_file()
    for artifact in metadata["artifacts"].values():
        assert len(artifact["sha256"]) == 64

    restored = tmp_path / "restored"
    _git(tmp_path, "clone", str(repo), str(restored))
    _git(restored, "reset", "--hard", head)

    staged = (capture / "staged.patch").read_bytes()
    if staged:
        _git(restored, "apply", "--index", "-", input_bytes=staged)

    unstaged = (capture / "unstaged.patch").read_bytes()
    if unstaged:
        _git(restored, "apply", "-", input_bytes=unstaged)

    with tarfile.open(capture / "untracked.tar", mode="r") as archive:
        archive.extractall(restored, filter="data")

    assert (restored / "tracked.txt").read_text(encoding="utf-8") == "base\nunstaged\n"
    assert (restored / "staged.txt").read_text(encoding="utf-8") == "staged\n"
    assert (restored / "nested" / "untracked.bin").read_bytes() == b"\x00\x01benchmark"
    assert (restored / "untracked-link").is_symlink()
    assert (restored / "untracked-link").readlink() == Path("tracked.txt")


def test_snapshot_refuses_destination_inside_repo(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)

    with pytest.raises(BenchmarkCaptureError, match="outside the repository"):
        snapshot_git_state(repo, repo / ".benchmark-capture")
