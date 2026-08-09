"""Tests for the peer-deployer path-safety allowlist.

The path-safety module is the single source of truth for which
filesystem paths the deployer is allowed to mutate. It refuses
generic operations on:

  * /
  * /opt
  * /opt/omnigent
  * /opt/omnigent/venv (the active runtime)
  * /opt/omnigent-production (O2's deployment root)
  * /var, /etc, /home, /root, /tmp
  * paths with ".." traversal
  * paths that resolve via symlink to a forbidden path
  * paths in O2's DB home
  * paths in O2's release root
  * empty / whitespace / NUL-byte paths
"""
from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
PKG_ROOT = REPO_ROOT / "deploy" / "scripts" / "peer_deployer"


def _load_pkg():
    if "peer_deployer" in sys.modules:
        return sys.modules["peer_deployer"]
    init = PKG_ROOT / "__init__.py"
    spec = importlib.util.spec_from_file_location(
        "peer_deployer", init,
        submodule_search_locations=[str(PKG_ROOT)],
    )
    assert spec is not None
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["peer_deployer"] = pkg
    spec.loader.exec_module(pkg)
    for name in (
        "identity",
        "transaction",
        "service_state",
        "preflight",
        "rollback",
        "staging",
        "path_safety",
        "fsm",
        "reconcile",
    ):
        sub_spec = importlib.util.spec_from_file_location(
            f"peer_deployer.{name}", PKG_ROOT / f"{name}.py"
        )
        assert sub_spec is not None
        sub = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"peer_deployer.{name}"] = sub
        sub_spec.loader.exec_module(sub)
        setattr(pkg, name, sub)
    return pkg


_pkg = _load_pkg()
path_safety = _pkg.path_safety
identity = _pkg.identity


def _set_o1_deployment_root(tmp_path: Path) -> Path:
    """Set O1's deployment_root to a tmp dir for the duration of a test."""
    o1 = identity.O1
    original = o1.deployment_root
    new_root = tmp_path / "o1"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(o1, "deployment_root", new_root)
    return original


def _restore_o1_deployment_root(original: Path) -> None:
    object.__setattr__(identity.O1, "deployment_root", original)


def _set_o2_deployment_root(tmp_path: Path) -> Path:
    """Set O2's deployment_root to a tmp dir for the duration of a test."""
    o2 = identity.O2
    original = o2.deployment_root
    new_root = tmp_path / "o2"
    new_root.mkdir(parents=True, exist_ok=True)
    object.__setattr__(o2, "deployment_root", new_root)
    return original


def _restore_o2_deployment_root(original: Path) -> None:
    object.__setattr__(identity.O2, "deployment_root", original)


class TestIntrinsicForbidden:
    """The intrinsic-forbidden list must always be rejected."""

    @pytest.mark.parametrize("path", [
        Path("/"),
        Path("/opt"),
        Path("/opt/omnigent"),
        Path("/opt/omnigent/venv"),
        Path("/opt/omnigent-production"),
        Path("/var"),
        Path("/var/lib"),
        Path("/var/lib/omnigent"),
        Path("/var/lib/omnigent-production"),
        Path("/etc"),
        Path("/etc/systemd"),
        Path("/etc/omnigent"),
        Path("/etc/omnigent-production"),
        Path("/home"),
        Path("/root"),
        Path("/tmp"),
        Path("/usr"),
        Path("/bin"),
        Path("/proc"),
        Path("/sys"),
        Path("/dev"),
        Path("/run"),
    ])
    def test_intrinsic_forbidden_rejected(self, path: Path) -> None:
        with pytest.raises(path_safety.PathSafetyError):
            path_safety.assert_on_allowlist(
                path,
                operation="delete",
                target=identity.O1,
                supervisor=identity.O2,
            )

    def test_o1_venv_is_intrinsic_forbidden(
        self, tmp_path: Path,
    ) -> None:
        """The canonical O1 active runtime path is forbidden."""
        original = _set_o1_deployment_root(tmp_path)
        try:
            with pytest.raises(path_safety.PathSafetyError):
                path_safety.assert_on_allowlist(
                    identity.O1.deployment_root / "venv",
                    operation="delete",
                    target=identity.O1,
                    supervisor=identity.O2,
                )
        finally:
            _restore_o1_deployment_root(original)


class TestTraversalAndEmptyPaths:
    """Traversal, empty, and malformed paths must be rejected."""

    def test_rejects_empty_path(self) -> None:
        with pytest.raises(path_safety.PathSafetyError, match="empty"):
            path_safety.assert_on_allowlist(
                "", operation="delete",
                target=identity.O1, supervisor=identity.O2,
            )

    def test_rejects_whitespace_path(self) -> None:
        with pytest.raises(path_safety.PathSafetyError):
            path_safety.assert_on_allowlist(
                "   ", operation="delete",
                target=identity.O1, supervisor=identity.O2,
            )

    def test_rejects_path_with_nul(self) -> None:
        with pytest.raises(path_safety.PathSafetyError, match="NUL"):
            path_safety.assert_on_allowlist(
                "/opt/omnigent\x00/test", operation="delete",
                target=identity.O1, supervisor=identity.O2,
            )

    def test_rejects_double_dot_even_in_valid_subpath(
        self, tmp_path: Path,
    ) -> None:
        """A path that uses '..' to escape its parent is rejected."""
        original = _set_o1_deployment_root(tmp_path)
        try:
            sneaky = identity.O1.deployment_root / "staging" / "tx1" / ".." / ".." / "venv"
            with pytest.raises(path_safety.PathSafetyError):
                path_safety.assert_on_allowlist(
                    sneaky, operation="delete",
                    target=identity.O1, supervisor=identity.O2,
                )
        finally:
            _restore_o1_deployment_root(original)

    def test_rejects_symlink_to_protected_path(
        self, tmp_path: Path,
    ) -> None:
        """A symlink to a protected path is rejected."""
        original = _set_o1_deployment_root(tmp_path)
        try:
            protected = identity.O1.deployment_root / "venv"
            protected.mkdir()
            safe_looking = identity.O1.deployment_root / "staging" / "tx1" / "fake_release"
            safe_looking.parent.mkdir(parents=True, exist_ok=True)
            os.symlink(protected, safe_looking)
            with pytest.raises(path_safety.PathSafetyError, match="active runtime"):
                path_safety.assert_on_allowlist(
                    safe_looking, operation="delete",
                    target=identity.O1, supervisor=identity.O2,
                )
        finally:
            _restore_o1_deployment_root(original)


class TestO2Protection:
    """O2's deployment root, release, and DB are protected."""

    def test_o2_deployment_root_protected(
        self, tmp_path: Path,
    ) -> None:
        """The O2 deployment root itself is protected."""
        with pytest.raises(path_safety.PathSafetyError, match="intrinsic-forbidden"):
            path_safety.assert_on_allowlist(
                Path("/opt/omnigent-production"),
                operation="delete",
                target=identity.O1,
                supervisor=identity.O2,
            )

    def test_o2_release_path_protected(
        self, tmp_path: Path,
    ) -> None:
        """A path under O2's releases/ is protected against deletion."""
        original = _set_o2_deployment_root(tmp_path)
        try:
            (identity.O2.deployment_root / "releases").mkdir()
            release = identity.O2.deployment_root / "releases" / ("deadbeef" * 5)
            release.mkdir(parents=True, exist_ok=True)
            with pytest.raises(path_safety.PathSafetyError, match="O2"):
                path_safety.assert_on_allowlist(
                    release, operation="delete",
                    target=identity.O1, supervisor=identity.O2,
                )
        finally:
            _restore_o2_deployment_root(original)

    def test_o2_db_path_protected(
        self, tmp_path: Path,
    ) -> None:
        """A path under O2's DB home is protected."""
        original = _set_o2_deployment_root(tmp_path)
        new_home = tmp_path / "o2_home"
        # The home is keyed by the deployment root; create a mapping
        # entry and remember the key for cleanup.
        new_root = identity.O2.deployment_root
        identity.HOME_MAPPING[str(new_root)] = new_home
        try:
            db = tmp_path / "o2_home" / "chat.db"
            db.parent.mkdir(parents=True, exist_ok=True)
            db.write_text("x")
            with pytest.raises(path_safety.PathSafetyError, match="O2"):
                path_safety.assert_on_allowlist(
                    db, operation="delete",
                    target=identity.O1, supervisor=identity.O2,
                )
        finally:
            _restore_o2_deployment_root(original)
            identity.HOME_MAPPING.pop(str(new_root), None)


class TestHelpers:
    """The introspection helpers work correctly."""

    def test_is_o1_active_runtime_true(self, tmp_path: Path) -> None:
        original = _set_o1_deployment_root(tmp_path)
        try:
            active = identity.O1.deployment_root / "venv"
            assert path_safety.is_o1_active_runtime(active, identity.O1) is True
        finally:
            _restore_o1_deployment_root(original)

    def test_is_o1_active_runtime_false(self, tmp_path: Path) -> None:
        original = _set_o1_deployment_root(tmp_path)
        try:
            not_active = identity.O1.deployment_root / "releases" / "abc"
            assert path_safety.is_o1_active_runtime(not_active, identity.O1) is False
        finally:
            _restore_o1_deployment_root(original)

    def test_is_o2_release_path_true(self, tmp_path: Path) -> None:
        original = _set_o2_deployment_root(tmp_path)
        try:
            (identity.O2.deployment_root / "releases").mkdir()
            release = identity.O2.deployment_root / "releases" / "abc"
            assert path_safety.is_o2_release_path(release, identity.O2) is True
        finally:
            _restore_o2_deployment_root(original)

    def test_is_o2_db_path_true(self, tmp_path: Path) -> None:
        original = _set_o2_deployment_root(tmp_path)
        new_home = tmp_path / "o2_home"
        identity.HOME_MAPPING[str(tmp_path / "o2")] = new_home
        try:
            db = tmp_path / "o2_home" / "chat.db"
            assert path_safety.is_o2_db_path(db, identity.O2) is True
        finally:
            _restore_o2_deployment_root(original)
            identity.HOME_MAPPING.pop(str(tmp_path / "o2"), None)
