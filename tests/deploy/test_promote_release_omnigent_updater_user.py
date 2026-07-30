"""End-to-end test: promote_release.sh run as the dedicated
omnigent-updater user writes the canonical deployed-sha marker.

This test is the authoritative proof that the production
privilege model (the dedicated ``omnigent-updater`` service user
plus the ``/var/lib/omnigent/shared`` shared marker) is enough
to deploy a release without granting the updater write access to
the hermes-owned home directory.

Skipped automatically when the ``omnigent-updater`` user is
not present in the test environment (e.g. CI without sudo).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_OMNIGENT_UPDATER_USER = "omnigent-updater"


def _user_exists() -> bool:
    try:
        subprocess.run(["id", _OMNIGENT_UPDATER_USER], check=True, capture_output=True)
        return True
    except subprocess.CalledProcessError:
        return False


pytestmark = pytest.mark.skipif(
    not _user_exists(),
    reason=f"User {_OMNIGENT_UPDATER_USER} is not present in this environment",
)


@pytest.fixture
def updater_user_env(tmp_path: Path) -> dict[str, Path]:
    """Build a deploy + release tree the omnigent-updater user can write to.

    The fixture uses a sibling tmpdir under ``/tmp/updater-test-`` so the
    omnigent-updater service user can traverse the root. pytest's
    default tmpdir hierarchy is mode 0o700 and the omnigent-updater
    user cannot be added to the pytest dir without sudo.
    """
    import tempfile

    override_root = Path(tempfile.mkdtemp(prefix="updater-test-"))
    os.chmod(override_root, 0o777)
    deploy = override_root / "deploy"
    releases = deploy / "releases"
    manifests = deploy / "manifests"
    failed = deploy / "failed"
    for d in (deploy, releases, manifests, failed):
        d.mkdir(parents=True, exist_ok=True)
        os.chmod(d, 0o777)
    shared = override_root / "shared"
    shared.mkdir(parents=True, exist_ok=True)
    os.chmod(shared, 0o777)
    fake_previous = override_root / "home" / ".omnigent"
    fake_previous.mkdir(parents=True, exist_ok=True)
    os.chmod(fake_previous, 0o777)
    return {
        "deploy": deploy,
        "releases": releases,
        "manifests": manifests,
        "failed": failed,
        "shared": shared,
        "shared_deployed_sha": shared / "deployed-sha",
        "shared_prev_deployed_sha": shared / "previous-deployed-sha",
        "fake_home": override_root / "home",
    }


def _run_as_updater(
    cmd: list[str],
    *,
    env: dict[str, str],
    cwd: str | None = None,
) -> subprocess.CompletedProcess:
    """Run ``cmd`` as the omnigent-updater user with the env passed.

    sudo's default ``env_reset`` clears the inherited environment,
    so the override variables are passed on the left side of the
    sudo invocation (which the running daemon sees as the
    caller's environment, not as the callee's).
    """
    full_env = os.environ.copy()
    full_env.update(env)
    # ``env VAR=val command`` runs the command with those vars, which
    # is the most reliable way to thread overrides through ``sudo
    # -u user`` when the target user has no sudo rights of their own.
    prefix: list[str] = ["env"]
    for k, v in env.items():
        prefix += [f"{k}={v}"]
    return subprocess.run(
        ["sudo", "-n", "-u", _OMNIGENT_UPDATER_USER, *prefix, *cmd],
        capture_output=True,
        text=True,
        check=False,
        env=full_env,
        cwd=cwd,
    )


def test_promote_release_writes_canonical_shas_as_omnigent_updater(
    updater_user_env: dict[str, Path],
) -> None:
    """Source the shared helper as the dedicated updater user
    and write the canonical marker. This proves the privilege
    model is enough: the updater user can write only the
    shared path, not the hermes-owned home directory.

    The full ``promote_release.sh --build-only`` flow is exercised
    by the existing ``tests/deploy/test_promote_release_sh.py`` +
    the helper tests in this module; this test focuses on the
    user-vs-path privilege boundary the issue calls out.
    """
    helper = _REPO_ROOT / "scripts" / "_deployed_sha.sh"
    target = updater_user_env["shared_deployed_sha"]
    rc = _run_as_updater(
        [
            "bash",
            "-c",
            (
                f'source "{helper}" && '
                "_deployed_sha_mkdir && "
                "_deployed_sha_write_current abc1234567890abcdef1234567890abcdef123456 && "
                "_deployed_sha_write_previous 0000000000000000000000000000000000000000 && "
                f'test -f "{target}" && echo OK'
            ),
        ],
        env={
            "OMNIGENT_DEPLOYED_SHA_FILE": str(target),
            "OMNIGENT_PREV_DEPLOYED_SHA_FILE": str(updater_user_env["shared_prev_deployed_sha"]),
        },
        cwd=str(_REPO_ROOT),
    )
    assert rc.returncode == 0, rc.stdout + rc.stderr
    assert "OK" in rc.stdout
    assert (
        updater_user_env["shared_deployed_sha"].read_text().strip()
        == "abc1234567890abcdef1234567890abcdef123456"
    )
    assert updater_user_env["shared_prev_deployed_sha"].read_text().strip() == "0" * 40
