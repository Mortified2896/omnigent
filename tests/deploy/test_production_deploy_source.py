"""Safety-contract tests for the Omnigent 2 production deploy controller."""

from __future__ import annotations

import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "deploy" / "scripts" / "deploy-omnigent-production.sh"


def test_unattended_source_deploy_requires_exact_sha(tmp_path: Path) -> None:
    """A no-argument invocation must fail before any deployment action."""
    result = subprocess.run(
        ["bash", str(SCRIPT)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    assert "requires an explicit full 40-character commit SHA" in result.stderr


def test_historical_bootstrap_branch_is_not_an_implicit_source() -> None:
    """The retired production bootstrap branch must not remain in the script."""
    source = SCRIPT.read_text()

    assert "bootstrap/omnigent-production-2" not in source
    assert "OMNIGENT_PROD_BRANCH" not in source


def test_source_resolution_requires_full_sha_and_logs_result() -> None:
    """The source gate accepts only an immutable SHA and reports its resolution."""
    source = SCRIPT.read_text()

    assert "^[0-9a-f]{40}$" in source
    assert 'git rev-parse --verify "${requested}^{commit}"' in source
    assert 'guard_log "resolved source SHA: $resolved"' in source


def test_maintenance_instance_guards_remain_present() -> None:
    """Source hardening must retain Omnigent 1 path, unit, and port guards."""
    source = SCRIPT.read_text()

    for protected in (
        'MAINTENANCE_PATHS=( "/opt/omnigent" "/etc/omnigent" "/var/lib/omnigent" )',
        'MAINTENANCE_SERVICES=( "omnigent.service" "omnigent-host.service" )',
        "MAINTENANCE_PORTS=(4097 9461)",
    ):
        assert protected in source
