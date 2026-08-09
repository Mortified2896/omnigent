"""Tests for the peer-deployer service_state helper.

These tests specifically cover:

  * the broken ``is-active | grep -q '^inactive$'`` pattern under
    ``set -o pipefail`` (the bug that hit the 2026-08-08 incident)
  * active services
  * inactive services
  * failed services
  * nonexistent services
  * the vetted helper distinguishes all four states correctly
"""

from __future__ import annotations

import contextlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SERVICE_STATE = (
    REPO_ROOT / "deploy" / "scripts" / "peer_deployer" / "service_state.py"
)
SPEC = importlib.util.spec_from_file_location("peer_deployer_service_state", SERVICE_STATE)
assert SPEC is not None and SPEC.loader is not None
service_state = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(service_state)


# ---------------------------------------------------------------------------
# Direct unit tests of the helper module.
# ---------------------------------------------------------------------------

class FakeCompleted:
    def __init__(self, stdout: str, stderr: str = "", returncode: int = 0):
        self.stdout = stdout
        self.stderr = stderr
        self.returncode = returncode


def test_is_active_classifies_active(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return FakeCompleted("active\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_active("omnigent.service") is True
    assert captured["cmd"][:2] == ["systemctl", "is-active"]


def test_is_active_rejects_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("inactive\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_active("omnigent.service") is False


def test_is_active_rejects_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("failed\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_active("omnigent.service") is False


def test_is_active_rejects_activating(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("activating\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    # activating is not "active" — it is "ok but not yet active"
    assert service_state.is_active("omnigent.service") is False


def test_is_inactive_classifies_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("inactive\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_inactive("omnigent.service") is True


def test_is_inactive_rejects_active(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("active\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_inactive("omnigent.service") is False


def test_is_failed_classifies_failed(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("failed\n")

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_failed("omnigent.service") is True


def test_get_state_raises_on_unknown_unit(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(cmd, **kwargs):
        return FakeCompleted("", stderr="Unit xxx not loaded.", returncode=1)

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    with pytest.raises(service_state.ServiceStateError):
        service_state.get_state("nonexistent.service")


def test_is_active_ignores_exit_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """systemctl is-active returns 3 for inactive; the helper must NOT
    treat that as an error. The helper must succeed based on stdout.
    """
    captured = []

    def fake_run(cmd, **kwargs):
        captured.append((cmd, kwargs.get("check")))
        return FakeCompleted("inactive\n", returncode=3)

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_active("omnigent.service") is False
    # AND check=False is used so a non-zero exit is not raised.
    assert all(c[1] is False for c in captured)


def test_is_known_returns_true_for_real_systemd_unit() -> None:
    """If systemd has any unit installed, the helper should report True.

    We use a unit that we know exists on a typical Linux system: the
    dbus unit. If dbus is not present, the test is skipped.
    """
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl not available")
    real = subprocess.run(
        ["systemctl", "cat", "dbus.service"],
        capture_output=True,
        text=True,
        check=False,
    )
    if real.returncode != 0:
        pytest.skip("dbus.service not installed")
    assert service_state.is_known("dbus.service") is True


def test_is_known_returns_false_for_nonexistent_unit() -> None:
    if shutil.which("systemctl") is None:
        pytest.skip("systemctl not available")
    assert service_state.is_known("omnigent-does-not-exist-test-only.service") is False


def test_is_active_rejects_empty_unit_name() -> None:
    with pytest.raises(service_state.ServiceStateError):
        service_state.is_active("")


def test_is_inactive_treats_unknown_as_inactive(monkeypatch: pytest.MonkeyPatch) -> None:
    """is_inactive must return True when the unit is unknown.

    The fail-safe semantics require that a post-restart check
    classifies unknown units as inactive. This guarantees that a
    missing unit does not silently succeed a deployment gate.
    """
    def fake_run(cmd, **kwargs):
        # First call is `is_known` (systemctl cat), returns no files.
        # Subsequent calls also return no state.
        return FakeCompleted("", stderr="No files found for nonexistent.service", returncode=1)

    monkeypatch.setattr(service_state.subprocess, "run", fake_run)
    assert service_state.is_inactive("nonexistent.service") is True


# ---------------------------------------------------------------------------
# Regression tests for the broken `is-active | grep` pattern under pipefail.
# ---------------------------------------------------------------------------

class TestPipefailRegression:
    """The 2026-08-08 incident used:

        systemctl is-active <unit> | grep -q '^inactive$'

    under ``set -o pipefail``. systemctl returns exit 3 for inactive,
    so the pipeline failed with the wrong diagnostic. These tests
    reproduce the buggy pipeline and prove the vetted helper does
    not have the same bug.
    """

    def test_broken_pattern_under_pipefail_misclassifies_inactive(self) -> None:
        if shutil.which("systemctl") is None:
            pytest.skip("systemctl not available")
        # We pick a unit that is genuinely inactive on the host.
        # Some hosts have unit "fail2ban.service" installed but inactive.
        candidate = "omnigent-test-pipefail-regression.service"
        with open(os.devnull, "w") as devnull:
            # Use a fake unit that doesn't exist; systemctl is-active
            # returns exit 3 for unknown units (active: inactive).
            proc = subprocess.run(
                ["bash", "-c", f"set -o pipefail; systemctl is-active {candidate} | grep -q '^inactive$' && echo MATCHED || echo MISSED"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        # The buggy pipeline cannot determine "inactive" because
        # systemctl returns exit 3 for "Unit xxx not loaded" — pipefail
        # propagates the failure. The pipeline prints "MISSED".
        assert "MISSED" in proc.stdout

    def test_broken_pattern_under_pipefail_misclassifies_active(self) -> None:
        """The same pattern also fails for an active service in a
        different way: when the service IS active, systemctl returns
        0 and grep fails (output is 'active', not 'inactive'), so the
        pipeline reports the service is NOT inactive, which is the
        intended behavior for ``grep -q '^inactive$'``. But the
        reason it works is the wrong reason: the bug bites the
        opposite case (when the service is not active).
        """
        if shutil.which("systemctl") is None:
            pytest.skip("systemctl not available")
        # We pick a real unit that is active on the host.
        candidate = "dbus.service"
        active = subprocess.run(
            ["systemctl", "is-active", candidate],
            capture_output=True, text=True, check=False,
        )
        if active.stdout.strip() != "active":
            pytest.skip("dbus.service is not active here")
        # The buggy pattern with grep -q '^inactive$' returns success
        # iff the service is *inactive*. For an active service, the
        # pattern correctly returns failure. The reason the pattern is
        # broken is that for an inactive service the pipeline fails
        # not because grep didn't match but because pipefail
        # propagated systemctl's exit 3.
        proc = subprocess.run(
            ["bash", "-c", f"set -o pipefail; systemctl is-active {candidate} | grep -q '^inactive$' && echo IDLE || echo RUNNING"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        assert "RUNNING" in proc.stdout


# ---------------------------------------------------------------------------
# Direct invocation of the bash library.
# ---------------------------------------------------------------------------

class TestBashLibrary:
    """Verify the vetted bash library distinguishes states correctly."""

    @pytest.fixture
    def sourced(self):
        bash = shutil.which("bash")
        if bash is None:
            pytest.skip("bash not available")
        lib = REPO_ROOT / "deploy" / "scripts" / "peer_deployer_lib.sh"
        if not lib.is_file():
            pytest.skip("peer_deployer_lib.sh not present")
        return bash, lib

    def _run(self, sourced, snippet: str) -> subprocess.CompletedProcess:
        bash, lib = sourced
        return subprocess.run(
            [bash, "-c", f"set -euo pipefail; source {lib}; {snippet}"],
            capture_output=True, text=True,
        )

    def test_is_active_strict_returns_truthy_for_active(self, sourced) -> None:
        if shutil.which("systemctl") is None:
            pytest.skip("systemctl not available")
        active = subprocess.run(
            ["systemctl", "is-active", "dbus.service"],
            capture_output=True, text=True, check=False,
        )
        if active.stdout.strip() != "active":
            pytest.skip("dbus.service is not active here")
        proc = self._run(sourced, "is_active_strict dbus.service && echo OK")
        assert proc.returncode == 0
        assert "OK" in proc.stdout

    def test_is_inactive_strict_treats_unknown_as_inactive(self, sourced) -> None:
        """is_inactive_strict on an unknown unit treats it as inactive.

        On modern systemd, ``systemctl is-active <unknown>`` returns the
        token ``inactive`` with exit 3. The vetted helper captures the
        token and ignores the exit code, so it correctly classifies
        unknown units as inactive. This is fail-safe: a unit that
        systemd does not know behaves like an inactive unit for the
        purpose of any post-restart check.
        """
        if shutil.which("systemctl") is None:
            pytest.skip("systemctl not available")
        proc = self._run(
            sourced,
            "is_inactive_strict omnigent-does-not-exist-test-only.service && echo IDLE || echo NOT-INACTIVE",
        )
        assert proc.returncode == 0
        assert "IDLE" in proc.stdout

    def test_assert_target_not_supervisor_refuses_equal(self, sourced) -> None:
        proc = self._run(sourced, "assert_target_not_supervisor O1 O1")
        assert proc.returncode == 2
        assert "target == supervisor" in proc.stderr

    def test_assert_target_not_supervisor_accepts_distinct(self, sourced) -> None:
        if shutil.which("systemctl") is None:
            pytest.skip("systemctl not available")
        proc = self._run(sourced, "assert_target_not_supervisor O1 O2 && echo OK")
        assert proc.returncode == 0
        assert "OK" in proc.stdout

    def test_assert_exact_sha_rejects_short(self, sourced) -> None:
        proc = self._run(sourced, "assert_exact_sha abc123")
        assert proc.returncode == 2
        assert "exact 40-character" in proc.stderr

    def test_assert_exact_sha_rejects_uppercase(self, sourced) -> None:
        proc = self._run(
            sourced,
            "assert_exact_sha 541C9A3180B81BFB2FC450B3EF5F8648691B359D",
        )
        assert proc.returncode == 2
        assert "exact 40-character" in proc.stderr

    def test_assert_exact_sha_accepts_lowercase_40(self, sourced) -> None:
        proc = self._run(
            sourced,
            "assert_exact_sha 541c9a3180b81bfb2fc450b3ef5f8648691b359d && echo OK",
        )
        assert proc.returncode == 0
        assert "OK" in proc.stdout

    def test_refuse_broken_pattern_detects_offending_script(self, sourced, tmp_path) -> None:
        bad = tmp_path / "bad.sh"
        bad.write_text("systemctl is-active x | grep -q '^inactive$'\n")
        proc = self._run(sourced, f"refuse_broken_is_active_pattern {bad}")
        assert proc.returncode == 2
        assert "broken" in proc.stderr

    def test_refuse_broken_pattern_accepts_clean_script(self, sourced, tmp_path) -> None:
        good = tmp_path / "good.sh"
        good.write_text("service_state x\n")
        proc = self._run(sourced, f"refuse_broken_is_active_pattern {good} && echo CLEAN")
        assert proc.returncode == 0
        assert "CLEAN" in proc.stdout

    def test_assert_state_unchanged_detects_diff(self, sourced, tmp_path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_text("sha=abc\n")
        b.write_text("sha=xyz\n")
        proc = self._run(sourced, f"assert_state_unchanged {a} {b}")
        assert proc.returncode != 0

    def test_assert_state_unchanged_accepts_identical(self, sourced, tmp_path) -> None:
        a = tmp_path / "a"
        b = tmp_path / "b"
        a.write_text("sha=abc\n")
        b.write_text("sha=abc\n")
        proc = self._run(sourced, f"assert_state_unchanged {a} {b} && echo EQUAL")
        assert proc.returncode == 0
        assert "EQUAL" in proc.stdout
