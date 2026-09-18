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
        capture_output=True,
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


@pytest.mark.parametrize("detached", [False, True])
def test_binary_index_worktree_and_clean_snapshot(tmp_path, detached):
    repo = tmp_path / "repo"
    _init_repo(repo)
    (repo / "binary").write_bytes(b"\x00base")
    _git(repo, "add", "binary")
    _git(repo, "commit", "-m", "binary")
    if detached:
        _git(repo, "checkout", "--detach")
    clean = snapshot_git_state(repo, tmp_path / "clean")
    assert clean["origin"] is None
    assert clean["detached"] is detached
    assert (tmp_path / "clean/status.z").read_bytes() == b""
    (repo / "binary").write_bytes(b"\x00staged")
    _git(repo, "add", "binary")
    (repo / "binary").write_bytes(b"\x00unstaged")
    index_before = (repo / ".git/index").read_bytes()
    capture = tmp_path / "binary-capture"
    snapshot_git_state(repo, capture)
    assert (repo / ".git/index").read_bytes() == index_before
    restored = tmp_path / "restored"
    _git(tmp_path, "clone", str(repo), str(restored))
    _git(restored, "apply", "--index", "-", input_bytes=(capture / "staged.patch").read_bytes())
    _git(restored, "apply", "-", input_bytes=(capture / "unstaged.patch").read_bytes())
    assert _git(restored, "show", ":binary") == b"\x00staged"
    assert (restored / "binary").read_bytes() == b"\x00unstaged"


def test_capture_disabled_no_git_or_artifacts(tmp_path, monkeypatch):
    from omnigent import benchmark_capture as module

    monkeypatch.delenv("OMNIGENT_BENCHMARK_CAPTURE", raising=False)
    monkeypatch.setattr(module, "_git", lambda *a, **kw: pytest.fail("disabled Git operation"))
    assert (
        module.TurnCapture.begin(
            cwd=str(tmp_path), session_id="s", turn_id="t", harness="codex", task_input={}
        )
        is None
    )
    assert list(tmp_path.iterdir()) == []


def test_stable_identity_commit_branch_change_and_export(tmp_path, monkeypatch):
    import json

    from omnigent.benchmark_capture import TurnCapture
    from omnigent.benchmark_capture_export import export_candidate

    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(tmp_path / "captures"))
    repo = tmp_path / "repo"
    head = _init_repo(repo)
    capture = TurnCapture.begin(
        cwd=str(repo),
        session_id="s",
        turn_id="response_1",
        harness="codex",
        task_input={
            "messages": [{"role": "user", "content": "Add a feature"}],
            "model": "requested",
        },
    )
    assert capture is not None
    capture_id = capture.manifest["capture_id"]
    _git(repo, "checkout", "-b", "changed")
    (repo / "tracked.txt").write_text("solution\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "solution")
    capture.finish("completed")
    capture.finish("failed")
    saved = json.loads((capture.directory / "manifest.json").read_text())
    assert saved["capture_id"] == capture_id
    assert saved["terminal_state"] == "completed"
    assert saved["start"]["head"] == head
    assert saved["end"]["head"] != head
    assert saved["end"]["branch"] == "changed"
    assert saved["requested_model"] == "requested"
    assert saved["observed_model"] is None
    assert all(value is None for value in saved["routing"].values())
    exported = export_candidate(capture.directory, tmp_path / "export")
    assert exported["dirty_start"] is False
    assert (
        json.loads((tmp_path / "export/provenance.jsonl").read_text())["content"]
        == "Add a feature"
    )


def test_concurrent_captures_and_symlink_root_rejection(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor

    from omnigent.benchmark_capture import TurnCapture

    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE", "1")
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(tmp_path / "captures"))
    repo = tmp_path / "repo"
    _init_repo(repo)

    def run(number):
        capture = TurnCapture.begin(
            cwd=str(repo),
            session_id=str(number),
            turn_id=str(number),
            harness="codex",
            task_input={},
        )
        capture.finish("completed")
        return capture.manifest["capture_id"]

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert len(set(pool.map(run, range(4)))) == 4
    alias = tmp_path / "alias"
    alias.symlink_to(repo, target_is_directory=True)
    monkeypatch.setenv("OMNIGENT_BENCHMARK_CAPTURE_DIR", str(alias / "capture"))
    assert (
        TurnCapture.begin(
            cwd=str(repo), session_id="s", turn_id="t", harness="codex", task_input={}
        )
        is None
    )
    assert not (repo / "capture").exists()


def test_rollout_identity_boundaries_and_preservation(tmp_path):
    import json

    from omnigent.benchmark_capture_codex import preserve_rollout

    home = tmp_path / "home"
    source = home / "sessions/2026/09/15/rollout-2026-09-15T00-00-00-thread-1.jsonl"
    source.parent.mkdir(parents=True)
    records = [
        {"type": "session_meta", "payload": {"id": "thread-1", "cli_version": "fixture"}},
        {"type": "turn_context", "payload": {"turn_id": "old", "model": "old-model"}},
        {"type": "event_msg", "payload": {"type": "task_started", "turn_id": "turn-2"}},
        {
            "type": "turn_context",
            "payload": {"turn_id": "turn-2", "model": "observed", "effort": "high"},
        },
        {"type": "event_msg", "payload": {"type": "task_complete", "turn_id": "turn-2"}},
    ]
    source.write_text("".join(json.dumps(record) + "\n" for record in records))
    evidence = preserve_rollout(
        home=home,
        explicit_path=None,
        thread_id="thread-1",
        turn_id="turn-2",
        destination=tmp_path / "preserved",
    )
    assert evidence["status"] == "preserved"
    assert evidence["turn_start_line"] == 3
    assert evidence["turn_end_line"] == 5
    assert evidence["model"] == "observed"
    assert (tmp_path / "preserved" / source.name).read_bytes() == source.read_bytes()
    records[0]["payload"]["id"] = "wrong"
    source.write_text(json.dumps(records[0]) + "\n")
    with pytest.raises(BenchmarkCaptureError, match="identity mismatch"):
        preserve_rollout(
            home=home,
            explicit_path=None,
            thread_id="thread-1",
            turn_id="turn-2",
            destination=tmp_path / "wrong",
        )
