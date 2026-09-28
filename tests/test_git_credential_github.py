"""Tests for the sandbox-side GitHub credential helper + host git setup."""

from __future__ import annotations

import io
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

import omnigent.git_credential_github as h
from omnigent.host.identity import HOST_TOKEN_ENV_VAR


@pytest.fixture(autouse=True)
def _force_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    # The broker host integrations are sandbox-only; default every test to the
    # in-sandbox path. The not-in-sandbox no-op test clears IS_SANDBOX explicitly.
    monkeypatch.setenv("IS_SANDBOX", "1")


class _FakeProxy:
    """Minimal direct-proxy handle for session-surface tests."""

    def __init__(self, tmp_path: Path, port: int) -> None:
        self.auth_token = f"proxy-auth-{port}"
        self.relay_port = port
        self.ca_bundle_path = tmp_path / f"ca-{port}.pem"
        self.ca_bundle_path.write_text("test-ca", encoding="utf-8")
        self.socket_path = tmp_path / f"proxy-{port}.sock"
        self.stopped = False

    def stop(self) -> None:
        self.stopped = True


def _fake_external_broker(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    port: int = 41001,
) -> tuple[h.GithubSessionBroker, dict[str, str], str, Path]:
    """Create a broker with a fake proxy but real session assets/scripts."""
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    home = tmp_path / "home"
    ssh_dir = home / ".ssh"
    ssh_dir.mkdir(parents=True)
    (ssh_dir / "id_ed25519").write_text("raw-ssh-private-key", encoding="utf-8")
    (home / ".git-credentials").write_text(
        "https://raw-git-token@example.test", encoding="utf-8"
    )
    (home / ".netrc").write_text(
        "machine github.com login x password raw-netrc-token", encoding="utf-8"
    )
    gh_dir = home / ".config" / "gh"
    gh_dir.mkdir(parents=True)
    (gh_dir / "hosts.yml").write_text("oauth_token: raw-gh-token", encoding="utf-8")

    fake_gh = tmp_path / "gh"
    fake_gh.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_gh.chmod(0o700)
    monkeypatch.setattr(
        h.shutil,
        "which",
        lambda name, path=None: (
            "/usr/bin/bwrap" if name == "bwrap" else str(fake_gh) if name == "gh" else None
        ),
    )
    monkeypatch.setattr(h, "_host_github_token", lambda _env: "gho-raw-host-token")
    monkeypatch.setattr(h, "_read_global_git_value", lambda _key: "owner@example.com")
    proxy = _FakeProxy(tmp_path, port)
    monkeypatch.setattr(h, "start_egress_proxy", lambda **_kwargs: proxy)
    broker = h.GithubSessionBroker.create(
        {
            "HOME": str(home),
            "XDG_CONFIG_HOME": str(home / ".config"),
            "PATH": "/usr/bin",
        }
    )
    assert broker is not None
    return broker, broker.runner_env(), "gho-raw-host-token", ssh_dir


def test_external_session_masks_ssh_and_keeps_raw_credentials_out_of_assets(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker, runner_env, raw_token, ssh_dir = _fake_external_broker(monkeypatch, tmp_path)
    try:
        wrapped = broker.wrap_runner_command(["python3", "-c", "pass"])
        assert str(ssh_dir) in wrapped
        assert "--unshare-pid" in wrapped
        assert "--unshare-uts" in wrapped
        assert "--unshare-ipc" in wrapped
        assert "--proc" in wrapped
        assert "SSH_AUTH_SOCK" not in runner_env
        assert runner_env["GH_TOKEN"] == runner_env[h.GITHUB_SESSION_TOKEN_ENV]
        assert runner_env["GIT_CONFIG_GLOBAL"] == runner_env[h.GITHUB_SESSION_GIT_CONFIG_ENV]
        assert runner_env["PATH"].startswith(runner_env[h.GITHUB_SESSION_BIN_ENV])
        assert raw_token not in runner_env.values()
        for path in Path(runner_env[h.GITHUB_SESSION_ROOT_ENV]).rglob("*"):
            if path.is_file():
                assert raw_token.encode() not in path.read_bytes()
                assert b"raw-ssh-private-key" not in path.read_bytes()
    finally:
        broker.close()


def test_external_session_git_helper_and_ssh_url_rewrite_work(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker, runner_env, raw_token, _ssh_dir = _fake_external_broker(monkeypatch, tmp_path)
    try:
        child_env = h.github_session_child_env({**runner_env, "PATH": "/usr/bin"})
        credential = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            text=True,
            capture_output=True,
            check=True,
            env=child_env,
        )
        assert "username=x-access-token" in credential.stdout
        assert child_env[h.GITHUB_SESSION_TOKEN_ENV] in credential.stdout
        assert raw_token not in credential.stdout

        repo = tmp_path / "repo"
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "remote",
                "add",
                "origin",
                "git@github.com:Mortified2896/omnigent.git",
            ],
            check=True,
        )
        rewritten = subprocess.run(
            ["git", "-C", str(repo), "ls-remote", "--get-url", "origin"],
            text=True,
            capture_output=True,
            check=True,
            env=child_env,
        )
        assert rewritten.stdout.strip() == "https://github.com/Mortified2896/omnigent.git"
    finally:
        broker.close()


def test_external_session_gh_wrapper_uses_synthetic_auth_and_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker, runner_env, raw_token, _ssh_dir = _fake_external_broker(monkeypatch, tmp_path)
    capture = tmp_path / "gh-env.txt"
    fake_gh = tmp_path / "gh"
    fake_gh.write_text(
        (
            "#!/bin/sh\nprintf '%s\\n%s\\n%s\\n' \"$GH_TOKEN\" \"$HTTPS_PROXY\" "
            "\"$SSL_CERT_FILE\" > \"$CAPTURE\"\n"
        ),
        encoding="utf-8",
    )
    fake_gh.chmod(0o700)
    # The helper asset points at the fake path selected during broker setup;
    # replace its final executable with the capture script in place.
    wrapper = Path(runner_env[h.GITHUB_SESSION_BIN_ENV]) / "gh"
    child_env = h.github_session_child_env({**runner_env, "PATH": "/usr/bin"})
    child_env["CAPTURE"] = str(capture)
    try:
        subprocess.run([str(wrapper), "pr", "create"], env=child_env, check=True)
        values = capture.read_text(encoding="utf-8").splitlines()
        assert values[0] == child_env[h.GITHUB_SESSION_TOKEN_ENV]
        assert values[1] == child_env[h.GITHUB_SESSION_PROXY_ENV]
        assert values[2] == child_env[h.GITHUB_SESSION_CA_ENV]
        assert raw_token not in capture.read_text(encoding="utf-8")
    finally:
        broker.close()


def test_external_sessions_are_isolated_and_do_not_touch_host_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    broker_a, env_a, _raw_a, _ssh_a = _fake_external_broker(
        monkeypatch, tmp_path / "a", port=41011
    )
    broker_b, env_b, _raw_b, _ssh_b = _fake_external_broker(
        monkeypatch, tmp_path / "b", port=41012
    )
    try:
        assert env_a[h.GITHUB_SESSION_ROOT_ENV] != env_b[h.GITHUB_SESSION_ROOT_ENV]
        assert env_a[h.GITHUB_SESSION_TOKEN_ENV] != env_b[h.GITHUB_SESSION_TOKEN_ENV]
        assert env_a[h.GITHUB_SESSION_PROXY_ENV] != env_b[h.GITHUB_SESSION_PROXY_ENV]
        assert (tmp_path / "a" / "home" / ".config" / "gh" / "hosts.yml").read_text(
            encoding="utf-8"
        ) == "oauth_token: raw-gh-token"
        assert (tmp_path / "b" / "home" / ".config" / "gh" / "hosts.yml").read_text(
            encoding="utf-8"
        ) == "oauth_token: raw-gh-token"
    finally:
        broker_a.close()
        broker_b.close()


def test_external_broker_is_noop_without_host_github_auth(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setattr(h, "_host_github_token", lambda _env: None)
    called = False

    def _unexpected_proxy(**_kwargs: object) -> None:
        nonlocal called
        called = True
        raise AssertionError("proxy must not start without host GitHub auth")

    monkeypatch.setattr(h, "start_egress_proxy", _unexpected_proxy)
    assert h.GithubSessionBroker.create({"PATH": "/usr/bin", "HOME": str(tmp_path)}) is None
    assert called is False


def test_managed_session_does_not_adopt_external_coordinates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    parent = {
        "IS_SANDBOX": "1",
        h.GITHUB_SESSION_ACTIVE_ENV: "1",
        h.GITHUB_SESSION_ROOT_ENV: "/tmp/session",
        h.GITHUB_SESSION_PROXY_ENV: "http://proxy",
        h.GITHUB_SESSION_CA_ENV: "/tmp/ca.pem",
        h.GITHUB_SESSION_GIT_CONFIG_ENV: "/tmp/gitconfig",
        h.GITHUB_SESSION_GH_CONFIG_ENV: "/tmp/gh",
        h.GITHUB_SESSION_TOKEN_ENV: "oa_cred_should_not_cross",
        h.GITHUB_SESSION_BIN_ENV: "/tmp/bin",
    }

    assert h.github_session_child_env(parent) == {}
    assert h.GithubSessionBroker.create(parent) is None


def test_get_prints_credentials_for_github(monkeypatch: pytest.MonkeyPatch) -> None:
    cred = {"connected": True, "username": "x-access-token", "token": "T"}
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: cred)
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=github.com\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = h.main(["--server", "http://s", "--host-id", "hid", "--host-token", "tok", "get"])
    assert rc == 0
    assert "username=x-access-token" in out.getvalue()
    assert "password=T" in out.getvalue()


def test_get_declines_for_non_github_host(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("protocol=https\nhost=gitlab.com\n\n"))
    out = io.StringIO()
    monkeypatch.setattr(sys, "stdout", out)
    rc = h.main(["--server", "http://s", "--host-id", "hid", "--host-token", "tok", "get"])
    assert rc == 0 and out.getvalue() == ""  # declined → git falls through


def test_get_fails_closed_on_missing_host_or_non_https(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fail closed: an empty/missing host or a non-https protocol must decline
    # (no output) so the brokered token never leaks to an unintended host.
    for stdin in ("protocol=https\n\n", "protocol=http\nhost=github.com\n\n", "\n"):
        monkeypatch.setattr(sys, "stdin", io.StringIO(stdin))
        out = io.StringIO()
        monkeypatch.setattr(sys, "stdout", out)
        rc = h.main(["--server", "http://s", "--host-id", "h", "--host-token", "t", "get"])
        assert rc == 0 and out.getvalue() == ""


def test_store_and_erase_are_noops(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(""))
    for op in ("store", "erase"):
        assert h.main(["--server", "http://s", "--host-id", "h", "--host-token", "t", op]) == 0


def test_configure_host_git_resets_then_adds_broker_helper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    cred = {"connected": True, "owner": "alice@example.com", "login": "octo"}
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: cred)
    h.configure_host_git("http://srv", "host1")

    # The github.com helper chain is reset (empty value) BEFORE the broker helper
    # is --add'ed, so a wider-scope $GIT_TOKEN helper cannot shadow the broker.
    key = "credential.https://github.com.helper"
    reset_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--replace-all", key] and c[-1] == ""
    )
    add_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1]
    )
    assert reset_idx < add_idx
    # Commit identity attributed to the connected owner.
    flat = [" ".join(c) for c in calls]
    assert any("user.email alice@example.com" in c for c in flat)
    assert any("user.name octo" in c for c in flat)


def test_configure_host_git_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    h.configure_host_git("http://srv", "host1")
    assert calls == []  # no token → nothing configured


def test_configure_host_git_clears_stale_broker_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Scoping: a confirmed not-connected owner (shared-$GIT_TOKEN / local model)
    # must NOT install the broker. It must also CLEAR any stale broker a prior
    # (inconclusive) clone probe installed — otherwise the reset it left strands
    # in-session git behind a declining broker. So the one and only write is the
    # unset that restores the ambient/shared helper; no --add, no identity.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    h.configure_host_git("http://srv", "host1")
    key = "credential.https://github.com.helper"
    assert calls == [["git", "config", "--global", "--unset-all", key]]
    flat = [" ".join(c) for c in calls]
    assert not any("--add" in c for c in flat)
    assert not any("user.email" in c for c in flat)


def test_credential_url_targets_the_generic_provider_path() -> None:
    # The helper hits the provider-generic broker with provider=github.
    assert h._credential_url("http://s", "hid") == "http://s/v1/hosts/hid/credentials/github"
    assert h._credential_url("http://s/", "hid") == "http://s/v1/hosts/hid/credentials/github"


def test_fetch_sends_launch_token_as_header_not_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # Exercise the real request construction (the other tests stub _fetch): the
    # launch token must ride the header so it can't land in server access logs.
    seen: dict = {}

    class _Resp:
        status_code = 200

        def json(self) -> dict:
            return {"connected": True, "token": "T", "username": "x-access-token"}

    def fake_get(url: str, headers: dict, timeout: float) -> _Resp:
        seen["url"], seen["headers"] = url, headers
        return _Resp()

    monkeypatch.setattr(h.httpx, "get", fake_get)
    data = h._fetch("http://s", "hid", "launch-tok")
    assert data is not None and data["token"] == "T"
    assert seen["url"] == "http://s/v1/hosts/hid/credentials/github"
    assert seen["headers"][h.MANAGED_HOST_TOKEN_HEADER] == "launch-tok"
    assert "launch-tok" not in seen["url"]


def test_configure_clone_credentials_wires_broker_when_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": True, "owner": "a@b.com"})
    assert h.configure_clone_credentials("http://srv", "host1") is True
    key = "credential.https://github.com.helper"
    reset_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--replace-all", key] and c[-1] == ""
    )
    add_idx = next(
        i
        for i, c in enumerate(calls)
        if c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1]
    )
    assert reset_idx < add_idx


def test_configure_clone_credentials_noop_when_not_connected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Not connected → leave the ambient (image GIT_TOKEN) helper intact; no git config.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    assert h.configure_clone_credentials("http://srv", "host1") is False
    assert calls == []


def test_configure_clone_credentials_noop_without_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    assert h.configure_clone_credentials("http://srv", "host1") is False
    assert calls == []


def test_configure_clone_credentials_fails_closed_when_probe_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A transient probe failure (``_fetch`` -> None) is indistinguishable from
    # "not linked", so fail closed: install the broker anyway rather than let the
    # clone silently fall back to the shared $GIT_TOKEN identity for what may be a
    # linked owner. Only a *successful* ``connected: false`` keeps the fallback.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    calls: list[list[str]] = []
    monkeypatch.setattr(h.subprocess, "run", lambda args, **k: calls.append(args) or None)
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: None)
    assert h.configure_clone_credentials("http://srv", "host1") is True
    key = "credential.https://github.com.helper"
    assert any(
        c[:5] == ["git", "config", "--global", "--add", key] and "host1" in c[-1] for c in calls
    )


def test_configure_host_gh_writes_hosts_yml(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    # A connected owner: gh's hosts.yml is materialized with the brokered token
    # and the owner's login, 0600, so `gh api` authenticates as them.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(
        h,
        "_fetch",
        lambda *a, **k: {"connected": True, "token": "gho_user", "login": "octo"},
    )
    assert h.configure_host_gh("http://srv", "host1") is True
    hosts_path = tmp_path / "gh" / "hosts.yml"
    written = yaml.safe_load(hosts_path.read_text())
    assert written["github.com"] == {
        "oauth_token": "gho_user",
        "user": "octo",
        "git_protocol": "https",
    }
    # The credential file is owner-only (0600) — never a world-readable window.
    assert (hosts_path.stat().st_mode & 0o777) == 0o600


def test_configure_host_gh_preserves_other_hosts(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Merge, don't truncate: an existing GitHub Enterprise entry (or a second
    # account) must survive — hosts.yml is a multi-host map, and the host's
    # GH_CONFIG_DIR can resolve to the developer's real ~/.config/gh.
    gh_dir = tmp_path / "gh"
    gh_dir.mkdir()
    (gh_dir / "hosts.yml").write_text(
        "github.mycompany.com:\n    oauth_token: enterprise-tok\n    user: alice\n"
    )
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(gh_dir))
    monkeypatch.setattr(
        h, "_fetch", lambda *a, **k: {"connected": True, "token": "gho_user", "login": "octo"}
    )
    assert h.configure_host_gh("http://srv", "host1") is True
    written = yaml.safe_load((gh_dir / "hosts.yml").read_text())
    # The enterprise host survives; github.com is added.
    assert written["github.mycompany.com"]["oauth_token"] == "enterprise-tok"
    assert written["github.com"]["oauth_token"] == "gho_user"


def test_configure_host_gh_noop_when_not_connected(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # Not linked → leave any ambient gh auth untouched; write nothing.
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    monkeypatch.setattr(h, "_fetch", lambda *a, **k: {"connected": False})
    assert h.configure_host_gh("http://srv", "host1") is False
    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_configure_host_gh_noop_without_token(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.delenv(HOST_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    assert h.configure_host_gh("http://srv", "host1") is False
    assert not (tmp_path / "gh" / "hosts.yml").exists()


def test_host_integrations_are_noops_outside_sandbox(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # NOT in a managed sandbox → every auto-apply is a complete no-op even for a
    # connected owner, so a local `omnigent host` never touches the developer's
    # real ~/.gitconfig or ~/.config/gh.
    monkeypatch.delenv("IS_SANDBOX", raising=False)
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "launch-tok")
    monkeypatch.setenv("GH_CONFIG_DIR", str(tmp_path / "gh"))
    calls: list[object] = []
    monkeypatch.setattr(h.subprocess, "run", lambda *a, **k: calls.append(a))
    monkeypatch.setattr(
        h, "_fetch", lambda *a, **k: {"connected": True, "token": "t", "login": "o"}
    )
    h.configure_host_git("http://srv", "host1")
    assert h.configure_host_gh("http://srv", "host1") is False
    assert h.start_host_gh_refresh("http://srv", "host1") is None
    assert calls == []  # no git config writes
    assert not (tmp_path / "gh" / "hosts.yml").exists()  # no hosts.yml write


def test_gh_refresh_interval_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(h._GH_REFRESH_INTERVAL_ENV_VAR, raising=False)
    assert h._gh_refresh_interval_s() == h._GH_REFRESH_DEFAULT_S
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "60")
    assert h._gh_refresh_interval_s() == 60
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "garbage")
    assert h._gh_refresh_interval_s() == h._GH_REFRESH_DEFAULT_S


def test_start_host_gh_refresh_disabled_when_nonpositive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-positive interval disables the refresher (returns no thread).
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "0")
    assert h.start_host_gh_refresh("http://srv", "host1") is None


def test_start_host_gh_refresh_rewrites_hosts_on_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The daemon loop re-materializes hosts.yml each tick. Drive exactly one
    # refresh, then park the (daemon) thread harmlessly on a never-set event so
    # the loop neither spins nor raises.
    import threading as _threading

    calls: list[tuple[str, str]] = []
    refreshed = _threading.Event()
    parked = _threading.Event()
    monkeypatch.setenv(h._GH_REFRESH_INTERVAL_ENV_VAR, "1")

    def _record(server: str, host_id: str) -> bool:
        calls.append((server, host_id))
        refreshed.set()
        return True

    monkeypatch.setattr(h, "configure_host_gh", _record)

    ticks = {"n": 0}

    def fake_sleep(_secs: float) -> None:
        ticks["n"] += 1
        if ticks["n"] >= 2:  # after one refresh, park forever (daemon → harmless)
            parked.wait()

    monkeypatch.setattr(h.time, "sleep", fake_sleep)
    t = h.start_host_gh_refresh("http://srv", "host1")
    assert t is not None
    assert refreshed.wait(timeout=5), "refresher never re-materialized hosts.yml"
    assert calls == [("http://srv", "host1")]
