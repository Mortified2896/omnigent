"""Tests for the deploy-main-* promotion preflight.

The preflight (``omnigent.deploy.preflight``) is the single source of
truth for "does this worktree have a deployable web UI bundle?". The
deploy promotion script invokes it via ``python -m
omnigent.deploy.preflight <worktree>`` before reloading systemd, and the
server startup emits a loud ERROR log when the same check fails on a
not-API-only deployment. These tests pin both surfaces so a future
refactor cannot silently re-introduce the API-only fallback as a silent
degradation.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.deploy.preflight import (
    WebUIBundleMissingError,
    expected_web_ui_dir,
    expected_web_ui_index,
    is_api_only_deployment,
    startup_web_ui_check,
    verify_web_ui_bundle,
)
from omnigent.deploy.preflight import (
    main as preflight_main,
)


@pytest.fixture
def fake_worktree(tmp_path: Path) -> Path:
    """Build a worktree skeleton that satisfies the preflight.

    Mirrors the layout ``scripts/promote_main_deploy.sh`` produces after
    a successful ``npm run build``:
    ``<root>/omnigent/server/static/web-ui/{index.html,version.json,manifest.webmanifest}``.
    """
    bundle = expected_web_ui_dir(tmp_path)
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html>")
    (bundle / "version.json").write_text('{"build": "test"}')
    (bundle / "manifest.webmanifest").write_text("{}")
    return tmp_path


def test_expected_web_ui_paths(fake_worktree: Path) -> None:
    """Path helpers resolve to the canonical layout the server uses."""
    assert expected_web_ui_dir(fake_worktree) == (
        fake_worktree / "omnigent" / "server" / "static" / "web-ui"
    )
    assert expected_web_ui_index(fake_worktree) == (
        fake_worktree / "omnigent" / "server" / "static" / "web-ui" / "index.html"
    )


def test_verify_web_ui_bundle_passes_on_full_build(fake_worktree: Path) -> None:
    """A worktree with all three artifacts passes the preflight."""
    verify_web_ui_bundle(fake_worktree)


def test_verify_web_ui_bundle_raises_on_partial_build(tmp_path: Path) -> None:
    """A partial build (only ``index.html``) is rejected with a runbook."""
    bundle = expected_web_ui_dir(tmp_path)
    bundle.mkdir(parents=True)
    (bundle / "index.html").write_text("<!doctype html>")
    with pytest.raises(WebUIBundleMissingError) as exc_info:
        verify_web_ui_bundle(tmp_path)
    msg = str(exc_info.value)
    # The error message is a runbook: name the missing file, point at
    # the rebuild command, and explain the API-only opt-out.
    assert "version.json" in msg
    assert "manifest.webmanifest" in msg
    assert "npm run build" in msg
    assert "OMNIGENT_SKIP_WEB_UI" in msg


def test_verify_web_ui_bundle_raises_on_empty_worktree(tmp_path: Path) -> None:
    """An empty worktree (no bundle directory at all) is rejected."""
    with pytest.raises(WebUIBundleMissingError) as exc_info:
        verify_web_ui_bundle(tmp_path)
    assert "index.html" in str(exc_info.value)


def test_web_ui_bundle_missing_error_carries_worktree_root(tmp_path: Path) -> None:
    """The exception exposes the worktree path so logs can be correlated."""
    with pytest.raises(WebUIBundleMissingError) as exc_info:
        verify_web_ui_bundle(tmp_path)
    assert exc_info.value.worktree_root == tmp_path
    assert "version.json" in exc_info.value.missing


@pytest.mark.parametrize(
    "env_value,expected",
    [
        ("true", True),
        ("True", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("Yes", True),
        ("on", False),  # "on" is not a recognised truthy value
        ("", False),
        ("0", False),
        ("false", False),
    ],
)
def test_is_api_only_deployment_truthy_table(
    monkeypatch: pytest.MonkeyPatch, env_value: str, expected: bool
) -> None:
    """``OMNIGENT_SKIP_WEB_UI`` is recognised only as canonical truthy values."""
    monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", env_value)
    assert is_api_only_deployment() is expected


def test_is_api_only_deployment_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unset env var means a normal UI deployment."""
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    assert is_api_only_deployment() is False


def test_startup_web_ui_check_logs_loud_error_on_missing_bundle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A normal UI deployment without a bundle logs a loud ERROR.

    This is the durable signal that makes the silent API-only fallback
    impossible: even when the server still comes up (the fallback is
    preserved) systemd captures the ERROR in the unit journal and the
    deploy promotion script's parallel preflight aborts the promotion.
    """
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    web_ui_dist = expected_web_ui_dir(tmp_path)  # empty dir
    with caplog.at_level(logging.ERROR, logger="omnigent.deploy.preflight"):
        result = startup_web_ui_check(web_ui_dist, worktree_root=tmp_path)
    assert result is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert errors, "expected at least one ERROR log"
    assert any("OMNIGENT_SKIP_WEB_UI" in r.getMessage() for r in errors)
    assert any("npm run build" in r.getMessage() for r in errors)
    assert any(str(tmp_path) in r.getMessage() for r in errors)


def test_startup_web_ui_check_silent_on_api_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An intentional API-only deployment does NOT log the loud ERROR."""
    monkeypatch.setenv("OMNIGENT_SKIP_WEB_UI", "true")
    web_ui_dist = expected_web_ui_dir(tmp_path)
    with caplog.at_level(logging.ERROR, logger="omnigent.deploy.preflight"):
        result = startup_web_ui_check(web_ui_dist, worktree_root=tmp_path)
    assert result is False
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors, "API-only deployment should not log the loud ERROR"


def test_startup_web_ui_check_silent_on_full_build(
    fake_worktree: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A correctly-bundled worktree emits no ERROR log."""
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    web_ui_dist = expected_web_ui_dir(fake_worktree)
    with caplog.at_level(logging.ERROR, logger="omnigent.deploy.preflight"):
        result = startup_web_ui_check(web_ui_dist, worktree_root=fake_worktree)
    assert result is True
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors


def test_startup_web_ui_check_silent_when_no_web_ui_dist_supplied(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """When ``web_ui_dist`` is ``None`` the loud-ERROR is suppressed —
    installed wheels resolve the bundle via package-data and the test
    fixtures that monkeypatch ``_WEB_UI_DIST`` deliberately point it at
    non-existent paths. The check returns True so the call site can
    unconditionally proceed without the loud-ERROR firing on every
    test fixture."""
    monkeypatch.delenv("OMNIGENT_SKIP_WEB_UI", raising=False)
    with caplog.at_level(logging.ERROR, logger="omnigent.deploy.preflight"):
        result = startup_web_ui_check(None)
    assert result is True
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert not errors


def test_preflight_main_cli_succeeds(
    fake_worktree: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The CLI entry point exits 0 on a valid worktree."""
    rc = preflight_main([str(fake_worktree)])
    out = capsys.readouterr()
    assert rc == 0
    assert "web UI bundle OK" in out.out


def test_preflight_main_cli_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI entry point exits 1 with a runbook on a missing bundle."""
    rc = preflight_main([str(tmp_path)])
    captured = capsys.readouterr()
    assert rc == 1
    assert "npm run build" in captured.err
    assert "OMNIGENT_SKIP_WEB_UI" in captured.err


def test_preflight_main_cli_rejects_bad_args(capsys: pytest.CaptureFixture[str]) -> None:
    """The CLI exits 2 on the wrong number of arguments."""
    rc = preflight_main([])
    captured = capsys.readouterr()
    assert rc == 2
    assert "usage" in captured.err


def test_preflight_main_cli_invokable_via_runpy(fake_worktree: Path) -> None:
    """``python -m omnigent.deploy.preflight`` matches the bash script's invocation.

    The promotion script invokes the preflight via ``python3 -m
    omnigent.deploy.preflight <worktree>``; this pins the runpy path so
    a typo in the bash script explodes here rather than on production.
    """
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.deploy.preflight", str(fake_worktree)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert "web UI bundle OK" in proc.stdout


def test_preflight_main_cli_runpy_fails_on_missing_bundle(
    tmp_path: Path,
) -> None:
    """The runpy path exits non-zero when the bundle is missing."""
    proc = subprocess.run(
        [sys.executable, "-m", "omnigent.deploy.preflight", str(tmp_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "npm run build" in proc.stderr
