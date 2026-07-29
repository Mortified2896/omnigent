"""Regression test for the rollback service-name env var leak (issue #38).

The earlier ``scripts/rollback_release.sh`` invoked the systemd helper
``service_name()`` / ``service_port()`` with ``OMNIGENT_DEPLOY_SERVICE_NAME=x``
/ ``OMNIGENT_DEPLOY_SERVICE_PORT=x`` set in the helper's environment. The
helpers in ``omnigent.deploy.ops.systemd`` read those env vars first and
return them verbatim when set — so the live ``systemctl restart x``
literal that resulted would silently never reach the real unit. The
fix unsets both vars before invoking the helper so the built-in
defaults (``omnigent-eval-web.service`` / ``4097``) are returned.

This test pins both halves of the fix:

1. The shell script no longer sets the env vars to ``"x"`` (a literal
   grep asserts the buggy lines are absent).
2. When invoked from an environment that does not pre-set the override
   vars (the post-fix environment of the helper invocation inside the
   script), the helper returns the real service name.

The second half is the structural guarantee — the helper behaves
sanely regardless of what shell code happens to surround it.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest

from omnigent.deploy.ops import systemd

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ROLLBACK_SCRIPT = _REPO_ROOT / "scripts" / "rollback_release.sh"


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip the override env vars that change ``service_name`` / ``service_port``.

    Mirrors the post-fix environment the rollback script's ``unset``
    statement produces, inside the helper-invocation subprocess and
    inside the Python interpreter under test.
    """
    monkeypatch.delenv("OMNIGENT_DEPLOY_SERVICE_NAME", raising=False)
    monkeypatch.delenv("OMNIGENT_DEPLOY_SERVICE_PORT", raising=False)


def test_rollback_script_does_not_set_service_name_to_x() -> None:
    """The buggy ``OMNIGENT_DEPLOY_SERVICE_NAME="x"`` literal must be gone.

    The original flow was::

        SERVICE_NAME=$(OMNIGENT_DEPLOY_SERVICE_NAME="x" python3 -c "
        ...
        from omnigent.deploy.ops.systemd import service_name
        print(service_name())
        ")

    ``grep`` for the literal pattern of the bug to make sure the fix
    cannot quietly regress to a renamed form (e.g.
    ``OMNIGENT_DEPLOY_SERVICE_NAME='x'``) — the regex tolerates either
    single or double quotes around ``"x"``.
    """
    text = _ROLLBACK_SCRIPT.read_text()
    assert not re.search(
        r'OMNIGENT_DEPLOY_SERVICE_NAME\s*=\s*["\']x["\']',
        text,
    ), (
        "rollback_release.sh must not pre-set OMNIGENT_DEPLOY_SERVICE_NAME=x; "
        "service_name() returns the env value verbatim when set, which "
        "silently points systemctl at a non-existent unit"
    )
    assert not re.search(
        r'OMNIGENT_DEPLOY_SERVICE_PORT\s*=\s*["\']x["\']',
        text,
    ), (
        "rollback_release.sh must not pre-set OMNIGENT_DEPLOY_SERVICE_PORT=x; "
        "service_port() returns the env value verbatim when set, which "
        "causes the loopback probe to hit the wrong port"
    )


def test_helper_returns_real_service_name_from_rollback_env(clean_env: None) -> None:
    """``service_name()`` resolves to ``omnigent-eval-web.service`` in the
    rollback script's helper invocation environment.

    Equivalent to the post-fix fallback inside
    ``scripts/rollback_release.sh`` after the new ``unset`` line runs.
    """
    assert systemd.service_name() == "omnigent-eval-web.service"


def test_helper_returns_real_service_port_from_rollback_env(clean_env: None) -> None:
    """``service_port()`` resolves to ``4097`` in the rollback script's
    helper invocation environment.

    The helper falls back to the documented default when the env var
    is unset or empty.
    """
    assert systemd.service_port() == 4097


def test_rollback_subprocess_resolves_correct_service_name(tmp_path: Path) -> None:
    """The roll-back script's helper subprocess resolves to the real
    unit, not ``"x"``.

    Reproduces the exact invocation pattern the fix introduced. The
    helper is invoked as a child process with no override env vars —
    the same post-fix state — and the captured stdout must be the
    canonical service name. The bash here is intentionally tiny so the
    test pins behaviour, not bash-internals; the real subprocess in
    the script is wrapped by ``$(...)`` and the helper import works
    identically.
    """
    if not _ROLLBACK_SCRIPT.is_file():
        pytest.skip("rollback_release.sh missing")
    helper_probe = (
        "unset OMNIGENT_DEPLOY_SERVICE_NAME OMNIGENT_DEPLOY_SERVICE_PORT; "
        f"PYTHONPATH={_REPO_ROOT!s} python3 -c "
        '"from omnigent.deploy.ops.systemd import service_name, service_port; '
        'print(service_name()); print(service_port())"'
    )
    proc = subprocess.run(
        ["bash", "-c", helper_probe],
        capture_output=True,
        text=True,
        check=False,
        env={
            "PATH": os.environ.get("PATH", ""),
            "HOME": os.environ.get("HOME", str(tmp_path)),
            "PYTHONPATH": str(_REPO_ROOT),
        },
    )
    assert proc.returncode == 0, proc.stderr
    lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    assert lines == ["omnigent-eval-web.service", "4097"], (
        f"helper returned {lines!r}; the rollback script would invoke "
        "`systemctl restart x` against a non-existent unit"
    )
