"""Static regression checks for the opt-in Control Room trusted-root bundle."""

from __future__ import annotations

import re
from pathlib import Path

POLICY_ROOT = Path(__file__).parents[2] / "deploy" / "control-room" / "trusted-root"
HOST_CONFIGS = {
    "O1": POLICY_ROOT / "systemd/omnigent-host.service.d/99-trusted-root.conf",
    "O2": POLICY_ROOT / "systemd/omnigent-production-host.service.d/99-trusted-root.conf",
}
ENV_CONFIGS = {
    "O1": POLICY_ROOT / "env/omnigent-trusted-root.env",
    "O2": POLICY_ROOT / "env/omnigent-production-trusted-root.env",
}


def _has_assignment(text: str, name: str, value: str) -> bool:
    return re.search(rf"^{re.escape(name)}={re.escape(value)}$", text, re.MULTILINE) is not None


def test_both_trusted_host_dropins_clear_root_blocking_sandbox_options() -> None:
    expected = {
        "NoNewPrivileges": "no",
        "RestrictSUIDSGID": "no",
        "PrivateUsers": "no",
        "ProtectSystem": "no",
        "ProtectHome": "no",
        "PrivateTmp": "no",
        "PrivateDevices": "no",
        "RestrictNamespaces": "no",
        "SystemCallArchitectures": "",
    }

    for instance, path in HOST_CONFIGS.items():
        text = path.read_text()
        for name, value in expected.items():
            assert _has_assignment(text, name, value), f"{instance} missing {name}={value}"

        assert not re.search(
            r"^(?:NoNewPrivileges|RestrictSUIDSGID)=((?:yes|true))$",
            text,
            re.MULTILINE | re.IGNORECASE,
        )
        assert not re.search(
            r"^SystemCallArchitectures=native$", text, re.MULTILINE | re.IGNORECASE
        )
        assert _has_assignment(text, "SystemCallFilter", "")
        assert _has_assignment(text, "CapabilityBoundingSet", "~")
        assert _has_assignment(text, "RestrictAddressFamilies", "")
        assert _has_assignment(text, "RestrictFileSystems", "")


def test_o2_trusted_dropin_cannot_retain_native_syscall_architecture() -> None:
    text = HOST_CONFIGS["O2"].read_text()

    assert "SystemCallArchitectures=native" not in text
    assert "SystemCallArchitectures=" in text
    assert "OMNIGENT_TRUSTED_ROOT_ACCESS=true" in text


def test_both_host_configs_and_env_fragments_carry_trusted_root_flag() -> None:
    for instance, path in HOST_CONFIGS.items():
        text = path.read_text()
        env_path = (
            "/etc/omnigent/trusted-root.env"
            if instance == "O1"
            else "/etc/omnigent-production/trusted-root.env"
        )
        assert _has_assignment(text, "EnvironmentFile", env_path)
        assert 'Environment="OMNIGENT_TRUSTED_ROOT_ACCESS=true"' in text

    for instance, path in ENV_CONFIGS.items():
        assert _has_assignment(path.read_text(), "OMNIGENT_TRUSTED_ROOT_ACCESS", "true"), instance


def test_sudoers_source_and_installer_validate_before_install() -> None:
    sudoers = (POLICY_ROOT / "sudoers/99-omnigent-agent-root").read_text()
    installer = (POLICY_ROOT / "install.sh").read_text()

    assert "hermes ALL=(ALL:ALL) NOPASSWD: ALL" in sudoers
    assert '"$VISUDO" -cf "$candidate"' in installer
    assert "99-omnigent-agent-root" in installer
    assert installer.rfind('"$VISUDO" -cf "$candidate"') < installer.index(
        'install -D -m 0440 "$candidate"'
    )
