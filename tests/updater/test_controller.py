"""Controller tests (issue #38 §6, §7, §9).

The controller is exercised end-to-end with substituted hooks so
no real cutover happens in tests. Each test pins one observable
behavior — successful promotion, broken-candidate rollback,
exactly-once notification, crash recovery — without relying on
the live deploy root.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from omnigent.updater import layout
from omnigent.updater.controller import (
    ControllerConfig,
    ControllerHooks,
    HealthCheckFailedError,
    UpdaterController,
)
from omnigent.updater.protocol import (
    Authorization,
    RequestRecord,
    new_request_id,
)
from omnigent.updater.state_machine import UpdatePhase
from omnigent.updater.store import UpdaterStore


def _auth() -> Authorization:
    return Authorization(kind="operator", operator="tester")


def _record(target: str = "0" * 40, expected: str = "0" * 40) -> RequestRecord:
    return RequestRecord(
        request_id=new_request_id(),
        target_sha=target,
        expected_current_sha=expected,
        origin_session_id="conv_abc",
        origin_conversation_id="conv_abc",
        requested_by="operator:tester",
        created_at="2026-01-01T00:00:00Z",
        authorization=_auth(),
    )


def _hooks(
    *,
    build: subprocess.CompletedProcess | None = None,
    promote: subprocess.CompletedProcess | None = None,
    rollback: subprocess.CompletedProcess | None = None,
    health_probes=None,
    drain=None,
    rehearse=None,
    backup=None,
) -> ControllerHooks:
    ok = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")

    def _build(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return build or ok

    def _promote(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        return promote or ok

    def _rollback(_repo: Path, **_kw: object) -> subprocess.CompletedProcess:
        return rollback or ok

    def _drain(_rid: str) -> None:
        if drain is not None:
            drain(_rid)
        return

    def _health_probes(_sha: str) -> None:
        if health_probes is not None:
            health_probes(_sha)

    def _rehearse(_rid: str, _target: str) -> None:
        if rehearse is not None:
            rehearse(_rid, _target)

    def _backup(_rid: str) -> None:
        if backup is not None:
            backup(_rid)

    return ControllerHooks(
        build_only=_build,
        promote=_promote,
        rollback=_rollback,
        health_probes=_health_probes,
        drain=_drain,
        rehearse=_rehearse,
        backup=_backup,
    )


def _controller(
    *,
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    hooks: ControllerHooks | None = None,
    dry_run: bool = False,
) -> UpdaterController:
    cfg = ControllerConfig(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
        dry_run=dry_run,
        hooks=hooks or _hooks(),
    )
    return UpdaterController(cfg, store=UpdaterStore(state_root=state_root))


def test_successful_run_writes_succeeded_result(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A successful run writes a ``succeeded`` result with the deployed SHA."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    # Stub the live SHA so the post-cutover verification (which
    # reads the deployed-sha file) finds the expected target. The
    # validation phase reads ``live_sha_file`` directly, so an
    # empty live SHA + ``expected="0"*40`` is the matching case.
    record_with_target_live = record  # expected="0"*40 matches empty live
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    result = controller.run(record_with_target_live)
    assert result.final_status == "succeeded"
    assert result.target_sha == target
    assert result.deployed_sha == target
    on_disk = layout.result_path(record.request_id)
    assert on_disk.is_file()


def test_build_failure_records_failed_result(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A failing build hook records ``failed`` with the build phase."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    failing = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="boom")
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
        hooks=_hooks(build=failing),
    )
    result = controller.run(record)
    assert result.final_status == "failed"
    assert result.failure_phase == "building"
    assert "boom" in result.failure_reason


def test_health_check_failure_triggers_rollback(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A failed health probe triggers the rollback path and records ``rolled_back``."""
    target = make_commit("target")
    # Set the live SHA so the post-rollback verification (which
    # uses ``live_sha_file`` to confirm the previous release) finds
    # the target SHA, matching the rollback target.
    live_sha_file.write_text(target + "\n")
    record = _record(target=target, expected=target)
    UpdaterStore(state_root=state_root).create_request(record)
    probe_calls: list[str] = []

    def bad_probe(sha: str) -> None:
        probe_calls.append(sha)
        # Fail the post-cutover probe so the controller rolls back;
        # the rollback path's health verification uses the same
        # probe — accept that call so the rollback can succeed.
        if len(probe_calls) == 1:
            raise HealthCheckFailedError("/health returned 500")

    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
        hooks=_hooks(health_probes=bad_probe),
    )
    result = controller.run(record)
    assert result.final_status == "rolled_back"
    assert result.rollback_performed is True
    assert result.rollback_result == "succeeded"
    # The probe ran at least twice: once after the cutover and
    # once after the rollback.
    assert len(probe_calls) >= 2


def test_rollback_failure_records_rollback_failed(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A failing rollback hook records ``rollback_failed``."""
    target = make_commit("target")
    live_sha_file.write_text(target + "\n")
    record = _record(target=target, expected=target)
    UpdaterStore(state_root=state_root).create_request(record)
    failing = subprocess.CompletedProcess(args=[], returncode=1, stdout="", stderr="rollback boom")

    def bad_probe(sha: str) -> None:
        # Fail the post-cutover probe to enter the rollback path;
        # the rollback itself then fails because the hook returns
        # non-zero, producing a ``rollback_failed`` terminal state.
        raise HealthCheckFailedError("/health returned 500")

    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
        hooks=_hooks(rollback=failing, health_probes=bad_probe),
    )
    result = controller.run(record)
    assert result.final_status == "rollback_failed"
    assert result.rollback_performed is True
    assert result.rollback_result == "failed"


def test_request_record_rejects_malformed_sha_at_construction() -> None:
    """The schema rejects malformed SHAs at construction time."""
    with pytest.raises(ValueError):
        RequestRecord(
            request_id=new_request_id(),
            target_sha="0" * 39,
            expected_current_sha="0" * 40,
            origin_session_id=None,
            origin_conversation_id=None,
            requested_by="operator:tester",
            created_at="2026-01-01T00:00:00Z",
            authorization=_auth(),
        )


def test_stale_expected_current_request_is_rejected(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A request whose ``expected_current_sha`` is stale is rejected."""
    target = make_commit("target")
    live = "0123456789abcdef0123456789abcdef01234567"
    live_sha_file.write_text(live + "\n")
    record = _record(target=target, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    result = controller.run(record)
    assert result.final_status == "rejected"
    assert "live" in result.failure_reason.lower() or "stale" in result.failure_reason.lower()


def test_unknown_target_sha_request_is_rejected(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
) -> None:
    """A request whose target SHA does not exist on the fork is rejected."""
    live_sha_file.write_text("0" * 40 + "\n")
    record = _record(target="1111111111111111111111111111111111111111", expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    result = controller.run(record)
    assert result.final_status == "rejected"


def test_out_of_lineage_target_is_rejected(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A target outside the configured lineage anchor is rejected."""
    # Build a separate, unrelated repo where a commit is created
    # that is NOT a descendant of the lineage anchor.
    other_repo = tmp_path / "other_repo"
    other_repo.mkdir()
    subprocess.run(
        ["git", "-C", str(other_repo), "init", "--initial-branch=main", "-q"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "config", "user.email", "test@example.invalid"],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(other_repo), "config", "user.name", "Test"],
        check=True,
    )
    (other_repo / "X").write_text("x")
    subprocess.run(["git", "-C", str(other_repo), "add", "X"], check=True)
    subprocess.run(
        ["git", "-C", str(other_repo), "commit", "-m", "out of lineage", "-q"],
        check=True,
    )
    other_sha = subprocess.run(
        ["git", "-C", str(other_repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # Add the unrelated repo as a remote of the main repo so the
    # SHA is reachable on the "fork" while remaining outside the
    # lineage.
    subprocess.run(
        ["git", "-C", str(repo_root), "remote", "add", "other", str(other_repo)],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(repo_root), "fetch", "other", "-q"],
        check=True,
    )
    live_sha_file.write_text("0" * 40 + "\n")
    record = _record(target=other_sha, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    result = controller.run(record)
    assert result.final_status == "rejected"


def test_dry_run_does_not_invoke_subprocesses(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """``dry_run`` skips the build/promote/rollback subprocesses."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    calls: list[str] = []

    def fake_build(_repo: Path, _sha: str) -> subprocess.CompletedProcess:
        calls.append("build")
        raise RuntimeError("dry_run should not call this")

    controller = UpdaterController(
        ControllerConfig(
            state_root=state_root,
            deploy_root=deploy_root,
            repo_root=repo_root,
            dry_run=True,
            hooks=ControllerHooks(
                build_only=fake_build,
                promote=fake_build,
                rollback=fake_build,
                drain=lambda _rid: None,
                health_probes=lambda _sha: None,
                rehearse=lambda r, t: None,
                backup=lambda r: None,
            ),
        ),
        store=UpdaterStore(state_root=state_root),
    )
    result = controller.run(record)
    assert result.final_status == "succeeded"
    assert calls == []


def test_recover_non_terminal_returns_noop_for_terminal(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A terminal result short-circuits the recovery scan."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    UpdaterStore(state_root=state_root).create_request(record)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    controller.run(record)
    decisions = controller.recover_non_terminal()
    assert decisions == []


def test_recover_non_terminal_classifies_resume(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A checkpoint in ``queued`` classifies as a resume decision."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    store = UpdaterStore(state_root=state_root)
    store.create_request(record)
    store.write_checkpoint(record.request_id, UpdatePhase.QUEUED)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    decisions = controller.recover_non_terminal()
    assert len(decisions) == 1
    assert decisions[0].request_id == record.request_id
    assert decisions[0].action in {"resume", "resume_idle"}


def test_recover_non_terminal_classifies_verify_after_promote(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A checkpoint in ``promoting`` classifies as a verify decision."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    store = UpdaterStore(state_root=state_root)
    store.create_request(record)
    store.write_checkpoint(record.request_id, UpdatePhase.PROMOTING)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    decisions = controller.recover_non_terminal()
    assert decisions[0].action == "verify"


def test_recover_non_terminal_classifies_record_rollback(
    state_root: Path,
    deploy_root: Path,
    repo_root: Path,
    live_sha_file: Path,
    lineage_anchor: str,
    make_commit,
) -> None:
    """A checkpoint in ``rolling_back`` classifies as a rollback-record decision."""
    target = make_commit("target")
    record = _record(target=target, expected="0" * 40)
    store = UpdaterStore(state_root=state_root)
    store.create_request(record)
    store.write_checkpoint(record.request_id, UpdatePhase.ROLLING_BACK)
    controller = _controller(
        state_root=state_root,
        deploy_root=deploy_root,
        repo_root=repo_root,
    )
    decisions = controller.recover_non_terminal()
    assert decisions[0].action == "record_rollback"


# ---------------------------------------------------------------------------
# Host-service pinning probes (issue: omnigent-eval-host must run from the
# same release as omnigent-eval-web).
# ---------------------------------------------------------------------------


def _configure_host_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pids: list[str] | None = None,
) -> None:
    monkeypatch.setattr("shutil.which", lambda _name: "/bin/systemctl")
    reported_pids = iter(pids or ["12345", "12345"])

    def fake_run(
        args: list[str] | tuple[str, ...],
        *rest: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        a = tuple(args)
        base = tuple(x.rsplit("/", 1)[-1] for x in a[:3])
        if base == ("systemctl", "is-active", "--quiet"):
            return subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr="")
        if base == ("systemctl", "show", "-p"):
            return subprocess.CompletedProcess(
                args=a, returncode=0, stdout=next(reported_pids) + "\n", stderr=""
            )
        raise AssertionError(f"unexpected subprocess: {a!r}")

    monkeypatch.setattr("subprocess.run", fake_run)


def _host_cmdline(interpreter: Path, *command: str) -> bytes:
    argv = (str(interpreter), *(command or ("-P", "-m", "omnigent", "host")))
    return b"\0".join(arg.encode() for arg in argv) + b"\0"


def _host_controller(deploy: Path) -> UpdaterController:
    return UpdaterController(
        ControllerConfig(
            deploy_root=deploy,
            host_service_name="omnigent-eval-host.service",
            dry_run=False,
        )
    )


def test_check_host_pinned_passes_for_release_local_host_cmdline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    deploy = tmp_path / "deploy"
    expected_python = deploy / "releases" / sha / ".venv" / "bin" / "python"
    expected_python.parent.mkdir(parents=True)
    expected_python.touch()
    _configure_host_probe(monkeypatch)

    original_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path) -> bytes:
        if path == Path("/proc/12345/cmdline"):
            return _host_cmdline(expected_python)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    _host_controller(deploy)._check_host_pinned(sha)


@pytest.mark.parametrize("exe_behavior", ["external", "permission_error"])
def test_check_host_pinned_never_consults_proc_exe(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    exe_behavior: str,
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    deploy = tmp_path / "deploy"
    expected_python = deploy / "releases" / sha / ".venv" / "bin" / "python"
    expected_python.parent.mkdir(parents=True)
    expected_python.touch()
    _configure_host_probe(monkeypatch)
    original_read_bytes = Path.read_bytes
    original_resolve = Path.resolve

    def fake_read_bytes(path: Path) -> bytes:
        if path == Path("/proc/12345/cmdline"):
            return _host_cmdline(expected_python)
        return original_read_bytes(path)

    def guarded_resolve(path: Path, *args: object, **kwargs: object) -> Path:
        if str(path) == "/proc/12345/exe":
            if exe_behavior == "permission_error":
                raise PermissionError("exe denied")
            return Path("/home/hermes/.local/share/uv/python/cpython/bin/python")
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    monkeypatch.setattr(Path, "resolve", guarded_resolve)
    _host_controller(deploy)._check_host_pinned(sha)


@pytest.mark.parametrize(
    ("cmdline_factory", "match"),
    [
        (
            lambda expected, deploy: _host_cmdline(
                deploy / "releases" / ("f" * 40) / ".venv" / "bin" / "python"
            ),
            "not pinned to the target release",
        ),
        (
            lambda expected, deploy: _host_cmdline(expected, "-P", "-m", "other_module", "host"),
            "wrong command",
        ),
        (
            lambda expected, deploy: _host_cmdline(expected, "-P", "-m", "omnigent", "server"),
            "wrong command",
        ),
        (lambda expected, deploy: b"", "empty cmdline"),
        (lambda expected, deploy: b"python\0-P", "malformed cmdline"),
        (lambda expected, deploy: b"python\0\0", "malformed cmdline"),
        (lambda expected, deploy: b"\xff\0", "malformed cmdline"),
    ],
)
def test_check_host_pinned_rejects_invalid_cmdline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    cmdline_factory,
    match: str,
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    deploy = tmp_path / "deploy"
    expected_python = deploy / "releases" / sha / ".venv" / "bin" / "python"
    expected_python.parent.mkdir(parents=True)
    expected_python.touch()
    _configure_host_probe(monkeypatch)
    cmdline = cmdline_factory(expected_python, deploy)
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path) -> bytes:
        if path == Path("/proc/12345/cmdline"):
            return cmdline
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(HealthCheckFailedError, match=match):
        _host_controller(deploy)._check_host_pinned(sha)


@pytest.mark.parametrize("error", [FileNotFoundError("gone"), PermissionError("denied")])
def test_check_host_pinned_rejects_missing_or_unreadable_cmdline(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    error: OSError,
) -> None:
    _configure_host_probe(monkeypatch)
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path) -> bytes:
        if path == Path("/proc/12345/cmdline"):
            raise error
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(HealthCheckFailedError, match=r"could not read.*cmdline"):
        _host_controller(tmp_path)._check_host_pinned("0" * 40)


def test_check_host_pinned_rejects_zero_pid(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    _configure_host_probe(monkeypatch, pids=["0"])
    with pytest.raises(HealthCheckFailedError, match="has no MainPID"):
        _host_controller(tmp_path)._check_host_pinned("0" * 40)


def test_check_host_pinned_fails_closed_when_process_is_replaced(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    sha = "0123456789abcdef0123456789abcdef01234567"
    deploy = tmp_path / "deploy"
    expected_python = deploy / "releases" / sha / ".venv" / "bin" / "python"
    expected_python.parent.mkdir(parents=True)
    expected_python.touch()
    _configure_host_probe(monkeypatch, pids=["12345", "54321"])
    original_read_bytes = Path.read_bytes

    def fake_read_bytes(path: Path) -> bytes:
        if path == Path("/proc/12345/cmdline"):
            return _host_cmdline(expected_python)
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "read_bytes", fake_read_bytes)
    with pytest.raises(HealthCheckFailedError, match="MainPID changed"):
        _host_controller(deploy)._check_host_pinned(sha)


def test_check_host_pinned_fails_when_unit_inactive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The host pinning check raises when ``systemctl is-active`` reports
    the unit is not active.
    """
    cfg = ControllerConfig(
        deploy_root=tmp_path,
        host_service_name="omnigent-eval-host.service",
        dry_run=False,
    )
    controller = UpdaterController(cfg)
    monkeypatch.setattr("shutil.which", lambda _name: "/bin/systemctl")

    def fake_run(
        args: list[str] | tuple[str, ...],
        *rest: object,
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        a = tuple(args)
        base = tuple(x.rsplit("/", 1)[-1] for x in a[:3])
        if base == ("systemctl", "is-active", "--quiet"):
            return subprocess.CompletedProcess(
                args=a, returncode=3, stdout="", stderr="inactive\n"
            )
        return subprocess.CompletedProcess(args=a, returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", fake_run)
    with pytest.raises(HealthCheckFailedError, match="not active"):
        controller._check_host_pinned("0123456789abcdef0123456789abcdef01234567")


def test_check_host_pinned_skipped_in_dry_run(tmp_path: Path) -> None:
    """The host pinning check is a no-op when ``dry_run`` is set.

    Tests cannot rely on a real ``systemctl`` so the controller
    silently skips the probe rather than shelling out from a unit
    test.
    """
    cfg = ControllerConfig(
        deploy_root=tmp_path,
        host_service_name="omnigent-eval-host.service",
        dry_run=True,
    )
    controller = UpdaterController(cfg)
    # No subprocess.run patch needed: the dry-run path returns
    # immediately.
    controller._check_host_pinned("0123456789abcdef0123456789abcdef01234567")
