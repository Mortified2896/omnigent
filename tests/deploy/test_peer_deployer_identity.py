"""Tests for the peer-deployer identity helpers.

These tests prove the target/supervisor identity is hard-coded,
distinct, and consistent with the canonical O1/O2 layout.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"

def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"peer_deployer_{name}", PKG_ROOT / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module

identity = _load("identity")


def test_o1_o2_have_distinct_roots() -> None:
    assert identity.O1.deployment_root != identity.O2.deployment_root
    assert identity.O1.deployment_root == Path("/opt/omnigent")
    assert identity.O2.deployment_root == Path("/opt/omnigent-production")


def test_o1_o2_have_distinct_ports() -> None:
    assert identity.O1.port != identity.O2.port
    assert identity.O1.port == 4097
    assert identity.O2.port == 4197


def test_o1_o2_have_distinct_service_units() -> None:
    assert identity.O1.service_unit != identity.O2.service_unit
    assert identity.O1.service_unit == "omnigent.service"
    assert identity.O2.service_unit == "omnigent-production.service"


def test_o1_o2_have_distinct_host_units() -> None:
    assert identity.O1.host_unit != identity.O2.host_unit
    assert identity.O1.host_unit == "omnigent-host.service"
    assert identity.O2.host_unit == "omnigent-production-host.service"


def test_registry_contains_canonical_instances() -> None:
    assert set(identity.REGISTRY) == {"O1", "O2"}
    assert identity.get("O1") is identity.O1
    assert identity.get("O2") is identity.O2


def test_get_unknown_raises() -> None:
    with pytest.raises(identity.IdentityError):
        identity.get("O3")


def test_instance_rejects_empty_name() -> None:
    with pytest.raises(identity.IdentityError):
        identity.Instance(
            name="",
            deployment_root=Path("/tmp"),
            service_unit="x",
            host_unit="y",
            port=1,
            health_url="http://x",
        )


def test_instance_rejects_relative_deployment_root() -> None:
    with pytest.raises(identity.IdentityError):
        identity.Instance(
            name="x",
            deployment_root=Path("relative"),
            service_unit="x",
            host_unit="y",
            port=1,
            health_url="http://x",
        )


def test_instance_rejects_out_of_range_port() -> None:
    with pytest.raises(identity.IdentityError):
        identity.Instance(
            name="x",
            deployment_root=Path("/tmp"),
            service_unit="x",
            host_unit="y",
            port=0,
            health_url="http://x",
        )


def test_require_distinct_refuses_same_name() -> None:
    with pytest.raises(identity.IdentityError, match="target == supervisor"):
        identity.require_distinct(identity.O1, identity.O1)


def test_require_distinct_refuses_same_root() -> None:
    class _Stub:
        name = "O2"
        deployment_root = identity.O1.deployment_root
        service_unit = "different"
        host_unit = "different"
        port = 5000
        health_url = "http://x"

    with pytest.raises(identity.IdentityError, match="deployment_root"):
        identity.require_distinct(identity.O1, _Stub())


def test_require_distinct_refuses_same_service() -> None:
    class _Stub:
        name = "O2"
        deployment_root = Path("/opt/other")
        service_unit = "omnigent.service"  # same as O1
        host_unit = "different"
        port = 5000
        health_url = "http://x"

    with pytest.raises(identity.IdentityError, match="service_unit"):
        identity.require_distinct(identity.O1, _Stub())


def test_require_distinct_refuses_same_port() -> None:
    class _Stub:
        name = "O2"
        deployment_root = Path("/opt/other")
        service_unit = "different"
        host_unit = "different"
        port = identity.O1.port
        health_url = "http://x"

    with pytest.raises(identity.IdentityError, match="port"):
        identity.require_distinct(identity.O1, _Stub())


def test_require_distinct_accepts_o1_o2() -> None:
    # Should not raise.
    identity.require_distinct(identity.O1, identity.O2)


def test_home_mapping_contains_canonical_paths() -> None:
    assert str(identity.O1.deployment_root) in identity.HOME_MAPPING
    assert identity.HOME_MAPPING[str(identity.O1.deployment_root)] == Path("/var/lib/omnigent")
    assert identity.HOME_MAPPING[str(identity.O2.deployment_root)] == Path("/var/lib/omnigent-production")


def test_snapshots_equal_ignores_health() -> None:
    a = {
        "name": "O2",
        "deployment_root": "/opt/omnigent-production",
        "service_unit": "omnigent-production.service",
        "host_unit": "omnigent-production-host.service",
        "port": 4197,
        "health_url": "http://127.0.0.1:4197/health",
        "provenance_sha": "abc",
        "provenance_version": "0.9.0.dev0",
        "installed_sha": "abc",
        "installed_version": "0.9.0.dev0",
    }
    b = dict(a, health_ok=True)
    assert identity.snapshots_equal(a, b) is True
    c = dict(a, installed_sha="different")
    assert identity.snapshots_equal(a, c) is False


def test_read_provenance_parses(tmp_path: Path) -> None:
    (tmp_path / "PROVENANCE.txt").write_text(
        "sha=" + "a" * 40 + "\n"
        "package_version=0.9.0.dev0\n"
    )
    parsed = identity.read_provenance(tmp_path)
    assert parsed["sha"] == "a" * 40
    assert parsed["package_version"] == "0.9.0.dev0"


def test_read_provenance_rejects_missing(tmp_path: Path) -> None:
    with pytest.raises(identity.IdentityError):
        identity.read_provenance(tmp_path)


def test_read_provenance_rejects_non_sha(tmp_path: Path) -> None:
    (tmp_path / "PROVENANCE.txt").write_text(
        "sha=not-a-sha\n"
        "package_version=0.9.0.dev0\n"
    )
    with pytest.raises(identity.IdentityError):
        identity.read_provenance(tmp_path)


def test_http_health_ok_returns_bool_for_active_service() -> None:
    """If the O2 service is healthy on the host, this returns True."""
    import shutil
    import subprocess
    if shutil.which("curl") is None:
        pytest.skip("curl not available")
    result = subprocess.run(
        ["curl", "-fsS", "--max-time", "3", "http://127.0.0.1:4197/health"],
        capture_output=True, text=True, check=False,
    )
    if result.returncode != 0:
        pytest.skip("O2 not healthy on this host")
    assert identity.http_health_ok("http://127.0.0.1:4197/health") is True
    assert identity.http_health_ok("http://127.0.0.1:1/never-listening") is False
