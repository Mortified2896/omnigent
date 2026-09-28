"""Git credential helper that fetches the session's GitHub token from the server.

Installed in a managed sandbox as git's ``credential.helper`` for
``github.com``. On each HTTPS auth challenge git runs this with ``get`` and the
request on stdin; the helper calls the server's host-facing credential endpoint
(:mod:`omnigent.server.routes.host_credentials`, with ``provider=github``) over
the sandbox's existing authenticated channel and prints back ``username`` /
``password``.

Why this shape:
- For **git**, the **GitHub token is never persisted** in the sandbox — it's
  fetched fresh per git operation and lives only in this short-lived process's
  stdout. (The gh CLI has no per-op credential hook, so
  :func:`configure_host_gh` does materialize the token into gh's ``hosts.yml``
  at launch — a within-sandbox persistence the threat model below already
  admits, matching how ``~/.databrickscfg`` is written — and
  :func:`start_host_gh_refresh` re-writes it on an interval so gh stays fresh
  across the ~8h token expiry the git broker otherwise handles per-op.)
- It is **executor-agnostic**: every executor already starts the host with a
  server URL + launch token, so nothing GitHub-specific is injected per
  executor. The host bakes the endpoint coordinates (server URL, host id, launch
  token) into the ``credential.helper`` invocation, so the helper works
  regardless of whether the process running git inherited the runner's env.

The launch token *does* live in the git config (a lesser, host-scoped,
expiring credential already present in this disposable sandbox); the user's
GitHub token does not.

Threat model: this removes the GitHub token from disk/env, not the ability of
in-sandbox code to request one. Any process that can read the launch token (git
config / argv) can call the endpoint and obtain the owner's full-scope user
token for its lifetime; teardown stops future fetches but does not revoke an
already-fetched token. The trust boundary is the sandbox, not this helper.

Run as: ``git credential.helper`` →
``python -m omnigent.git_credential_github --server <url> --host-id <id>
--host-token <tok>``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import yaml

from omnigent.host.identity import HOST_TOKEN_ENV_VAR as _HOST_TOKEN_ENV_VAR
from omnigent.host.identity import MANAGED_HOST_TOKEN_HEADER
from omnigent.inner.credential_proxy import (
    SYNTHETIC_CREDENTIAL_PREFIX,
    CredentialRewriteRule,
)
from omnigent.inner.egress.controller import EgressProxyHandle, start_egress_proxy
from omnigent.inner.sandbox import cleanup_private_tmpdir

_TIMEOUT_S = 15.0

# Opaque, per-runner coordinates for the trusted external-host GitHub session.
# These names are deliberately kept separate from the ordinary HTTP proxy
# variables: the runner receives only these paths/placeholders plus the
# session-local Git/GH aliases, while child processes opt into standard proxy
# variables only for the GitHub command that needs them.
GITHUB_SESSION_ACTIVE_ENV = "OMNIGENT_GITHUB_SESSION_ACTIVE"
GITHUB_SESSION_ROOT_ENV = "OMNIGENT_GITHUB_SESSION_ROOT"
GITHUB_SESSION_PROXY_ENV = "OMNIGENT_GITHUB_SESSION_PROXY_URL"
GITHUB_SESSION_CA_ENV = "OMNIGENT_GITHUB_SESSION_CA_BUNDLE"
GITHUB_SESSION_GIT_CONFIG_ENV = "OMNIGENT_GITHUB_SESSION_GIT_CONFIG"
GITHUB_SESSION_GH_CONFIG_ENV = "OMNIGENT_GITHUB_SESSION_GH_CONFIG"
GITHUB_SESSION_TOKEN_ENV = "OMNIGENT_GITHUB_SESSION_TOKEN"
GITHUB_SESSION_BIN_ENV = "OMNIGENT_GITHUB_SESSION_BIN"

_GITHUB_SESSION_REQUIRED_ENV = (
    GITHUB_SESSION_ROOT_ENV,
    GITHUB_SESSION_PROXY_ENV,
    GITHUB_SESSION_CA_ENV,
    GITHUB_SESSION_GIT_CONFIG_ENV,
    GITHUB_SESSION_GH_CONFIG_ENV,
    GITHUB_SESSION_TOKEN_ENV,
    GITHUB_SESSION_BIN_ENV,
)

# The proxy is intentionally narrower than a general-purpose GitHub API
# tunnel. Git smart HTTP needs GET/HEAD/POST on github.com; the normal PR
# panel/workflow needs read API access plus the REST/GraphQL calls used by gh.
# Existing shell/GitHub policy checks still gate force-push and destructive
# commands before they execute.
_TRUSTED_GITHUB_EGRESS_RULES = (
    "GET,HEAD,POST github.com/**",
    "GET api.github.com/user",
    "GET api.github.com/user/**",
    "GET api.github.com/rate_limit",
    "GET api.github.com/repos/*/*",
    "GET api.github.com/repos/*/*/**",
    "POST api.github.com/repos/*/*/pulls",
    # gh pr list/view/create uses GitHub's GraphQL endpoint for repository
    # metadata and pull-request queries in addition to REST /pulls.
    "POST api.github.com/graphql",
)


def _in_sandbox(env: Mapping[str, str] | None = None) -> bool:
    """Whether we're running inside a managed sandbox (host image sets ``IS_SANDBOX=1``).

    The broker host integrations auto-materialize the owner's credentials into
    the host's git / gh config. That's only ever wanted in a managed sandbox — a
    disposable, per-session filesystem. A local ``omnigent host`` shares the
    developer's real ``~/.gitconfig`` / ``~/.config/gh``, so every auto-apply must
    be a no-op there. ``IS_SANDBOX=1`` is baked into the managed host image and
    the k8s pod spec; it is absent on a local host.
    """
    source = os.environ if env is None else env
    return (source.get("IS_SANDBOX") or "").strip() == "1"


def _gh_config_dir_for_env(env: Mapping[str, str]) -> Path:
    """Resolve the host ``gh`` config directory without changing the env."""
    override = (env.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return Path(override).expanduser()
    home = Path(env.get("HOME") or str(Path.home())).expanduser()
    return Path(env.get("XDG_CONFIG_HOME") or (home / ".config")) / "gh"


def _host_github_token(env: Mapping[str, str]) -> str | None:
    """Read the already-configured host ``gh`` token into trusted memory.

    The command's stdout is captured and never logged, serialized, or passed
    to a runner. A failure is deliberately indistinguishable from an
    unconfigured host so external sessions continue to work normally without
    GitHub auth when it is unavailable.
    """
    gh = shutil.which("gh", path=env.get("PATH"))
    if gh is None:
        return None
    try:
        completed = subprocess.run(
            [gh, "auth", "token", "--hostname", "github.com"],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
            env=dict(env),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    token = (completed.stdout or "").strip()
    if not token or any(char.isspace() for char in token):
        return None
    return token


class _GithubTokenProvider:
    """Refresh the host token in memory without exposing it to the runner."""

    def __init__(self, initial: str, env: Mapping[str, str]) -> None:
        self._token: str | None = initial
        self._env = dict(env)
        self._last_refresh = time.monotonic()
        self._lock = threading.Lock()

    def resolve(self) -> str:
        """Return the current token, refreshing it at most every five minutes."""
        with self._lock:
            if self._token is None:
                raise ValueError("GitHub session token provider is closed")
            if time.monotonic() - self._last_refresh >= 300.0:
                refreshed = _host_github_token(self._env)
                if refreshed:
                    self._token = refreshed
                self._last_refresh = time.monotonic()
            return self._token

    def close(self) -> None:
        """Drop the in-memory token and its host environment snapshot."""
        with self._lock:
            self._token = None
            self._env.clear()


def _session_env_values(parent_env: Mapping[str, str]) -> dict[str, str]:
    """Return validated opaque session coordinates, or an empty mapping."""
    if (parent_env.get(GITHUB_SESSION_ACTIVE_ENV) or "").strip() != "1":
        return {}
    if (parent_env.get("IS_SANDBOX") or "").strip() == "1":
        # The managed broker has its own endpoint and must never inherit an
        # external host's loopback proxy, even if an operator accidentally
        # forwards these names into a managed runner.
        return {}
    values = {name: (parent_env.get(name) or "") for name in _GITHUB_SESSION_REQUIRED_ENV}
    if any(not value for value in values.values()):
        return {}
    root = Path(values[GITHUB_SESSION_ROOT_ENV])
    if not root.is_absolute():
        return {}
    token = values[GITHUB_SESSION_TOKEN_ENV]
    if not token.startswith(SYNTHETIC_CREDENTIAL_PREFIX):
        return {}
    return values


def github_session_read_root(parent_env: Mapping[str, str]) -> Path | None:
    """Return the session asset root that an active sandbox may read."""
    values = _session_env_values(parent_env)
    return Path(values[GITHUB_SESSION_ROOT_ENV]) if values else None


def github_session_child_env(parent_env: Mapping[str, str]) -> dict[str, str]:
    """Build child-only Git/GitHub env from opaque runner coordinates.

    No raw GitHub credential or host dotfile is copied. The returned mapping
    contains a synthetic token, session-local config paths, and a per-session
    proxy URL carrying only the proxy's ephemeral authentication token.
    Standard ``HTTP_PROXY`` variables are intentionally not set here: doing so
    would route unrelated model traffic through a GitHub-only proxy.
    """
    values = _session_env_values(parent_env)
    if not values:
        return {}
    path = parent_env.get("PATH", "")
    session_bin = values[GITHUB_SESSION_BIN_ENV]
    child: dict[str, str] = {
        **values,
        "GH_TOKEN": values[GITHUB_SESSION_TOKEN_ENV],
        "GITHUB_TOKEN": values[GITHUB_SESSION_TOKEN_ENV],
        "GH_CONFIG_DIR": values[GITHUB_SESSION_GH_CONFIG_ENV],
        "GIT_CONFIG_GLOBAL": values[GITHUB_SESSION_GIT_CONFIG_ENV],
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_TERMINAL_PROMPT": "0",
        "PATH": f"{session_bin}{os.pathsep}{path}" if path else session_bin,
    }
    return child


def github_session_http_env(parent_env: Mapping[str, str]) -> dict[str, str]:
    """Return standard proxy/CA variables for one GitHub API subprocess."""
    values = _session_env_values(parent_env)
    if not values:
        return {}
    proxy = values[GITHUB_SESSION_PROXY_ENV]
    bundle = values[GITHUB_SESSION_CA_ENV]
    return {
        "HTTP_PROXY": proxy,
        "HTTPS_PROXY": proxy,
        "http_proxy": proxy,
        "https_proxy": proxy,
        "SSL_CERT_FILE": bundle,
        "REQUESTS_CA_BUNDLE": bundle,
        "CURL_CA_BUNDLE": bundle,
        "GIT_SSL_CAINFO": bundle,
    }


class GithubSessionBroker:
    """Per-runner external-host GitHub broker.

    The host reads its existing ``gh`` login once, keeps the real token in the
    host process, and starts a direct loopback egress proxy. The runner gets a
    fresh synthetic credential, an isolated Git config/helper, and a masked
    view of host credential directories. Managed sandboxes never construct this
    class; their existing server-backed broker remains unchanged.
    """

    def __init__(
        self,
        *,
        root: Path,
        proxy: EgressProxyHandle,
        provider: _GithubTokenProvider,
        synthetic: str,
        runner_env: dict[str, str],
        masked_paths: tuple[Path, ...],
        bwrap: str,
    ) -> None:
        self._root = root
        self._proxy = proxy
        self._provider = provider
        self._synthetic = synthetic
        self._runner_env = runner_env
        self._masked_paths = masked_paths
        self._bwrap = bwrap
        self._closed = False

    @classmethod
    def create(cls, parent_env: Mapping[str, str] | None = None) -> GithubSessionBroker | None:
        """Create a broker when the trusted host has usable GitHub auth."""
        env = dict(parent_env or os.environ)
        if _in_sandbox(env):
            return None
        bwrap = shutil.which("bwrap", path=env.get("PATH"))
        # The external path is fail-closed when the host cannot provide the
        # same filesystem masking guarantee as RTX Linux. Normal coding
        # sessions still start; they simply retain their existing no-GitHub-
        # broker behavior on unsupported hosts.
        if bwrap is None:
            return None
        token = _host_github_token(env)
        if token is None:
            return None

        root = Path(tempfile.mkdtemp(prefix="omnigent-github-session-"))
        os.chmod(root, 0o700)
        provider = _GithubTokenProvider(token, env)
        synthetic = f"{SYNTHETIC_CREDENTIAL_PREFIX}{secrets.token_urlsafe(24)}"
        rewrites = [
            CredentialRewriteRule(
                host="github.com",
                scheme="basic",
                synthetic=synthetic,
                username="x-access-token",
                secret_provider=provider.resolve,
            ),
            CredentialRewriteRule(
                host="api.github.com",
                scheme="token",
                synthetic=synthetic,
                secret_provider=provider.resolve,
            ),
        ]
        proxy: EgressProxyHandle | None = None
        try:
            proxy = start_egress_proxy(
                rules=_TRUSTED_GITHUB_EGRESS_RULES,
                tmpdir=root,
                allow_private_destinations=False,
                require_auth=True,
                credential_rewrites=rewrites,
                direct=True,
            )
            if not proxy.auth_token:
                raise RuntimeError("external GitHub proxy did not create auth")
            broker = cls.__new__(cls)
            broker._root = root
            broker._proxy = proxy
            broker._provider = provider
            broker._synthetic = synthetic
            broker._bwrap = bwrap
            broker._closed = False
            broker._masked_paths = broker._host_mask_paths(env)
            broker._write_assets(env)
            broker._runner_env = broker._build_runner_env(env)
            return broker
        except Exception:  # noqa: BLE001 — broker setup is best-effort
            if proxy is not None:
                with contextlib.suppress(Exception):
                    proxy.stop()
            provider.close()
            cleanup_private_tmpdir(root)
            return None

    def _host_mask_paths(self, env: Mapping[str, str]) -> tuple[Path, ...]:
        """Choose only credential surfaces to hide in the runner namespace."""
        home = Path(env.get("HOME") or str(Path.home())).expanduser()
        xdg = Path(env.get("XDG_CONFIG_HOME") or (home / ".config")).expanduser()
        candidates = (
            home / ".ssh",
            _gh_config_dir_for_env(env),
            home / ".gitconfig",
            home / ".git-credentials",
            home / ".netrc",
            xdg / "git",
        )
        unique: list[Path] = []
        configured_paths = [
            Path(value).expanduser()
            for key in ("GIT_CONFIG_GLOBAL", "NETRC")
            if (value := (env.get(key) or "").strip())
            and Path(value).expanduser().is_absolute()
        ]
        for path in (*candidates, *configured_paths):
            resolved = path.expanduser()
            if resolved not in unique and (resolved.exists() or resolved.parent.exists()):
                unique.append(resolved)
        return tuple(unique)

    def _write_assets(self, env: Mapping[str, str]) -> None:
        """Materialize placeholder-only Git/gh assets in the private root."""
        assert self._proxy.auth_token is not None
        bin_dir = self._root / "bin"
        gh_dir = self._root / "gh-config"
        bin_dir.mkdir(mode=0o700)
        gh_dir.mkdir(mode=0o700)

        gh = shutil.which("gh", path=env.get("PATH"))
        if gh is None:  # guarded by _host_github_token, defensive only
            raise RuntimeError("gh disappeared while creating GitHub session")
        proxy_url = (
            f"http://omnigent:{self._proxy.auth_token}@127.0.0.1:{self._proxy.relay_port}"
        )
        ca_bundle = str(self._proxy.ca_bundle_path)
        git_config = self._root / "gitconfig"
        user_lines: list[str] = []
        for key in ("user.name", "user.email"):
            value = _read_global_git_value(key)
            if value:
                section, name = key.split(".", 1)
                if not user_lines:
                    user_lines.append(f"[{section}]")
                user_lines.append(f"{name} = {_git_config_value(value)}")
        config_lines = [
            *user_lines,
            '[credential "https://github.com/"]',
            "    helper =",
            "    helper = omnigent-session",
            '[url "https://github.com/"]',
            "    insteadOf = git@github.com:",
            "    insteadOf = ssh://git@github.com/",
            "    insteadOf = git+ssh://git@github.com/",
            '[http "https://github.com/"]',
            f"    proxy = {proxy_url}",
            f"    sslCAInfo = {ca_bundle}",
            "",
        ]
        git_config.write_text("\n".join(config_lines), encoding="utf-8")
        os.chmod(git_config, 0o600)

        helper = bin_dir / "git-credential-omnigent-session"
        helper.write_text(
            """#!/bin/sh
case "${1-get}" in
  get)
    protocol=
    host=
    while IFS='=' read -r key value; do
      [ -z "$key" ] && break
      case "$key" in
        protocol) protocol=$value ;;
        host) host=$value ;;
      esac
    done
    [ "$protocol" = "https" ] || exit 0
    [ "$host" = "github.com" ] || exit 0
    [ -n "${OMNIGENT_GITHUB_SESSION_TOKEN:-}" ] || exit 0
    printf 'username=x-access-token\\npassword=%s\\n\\n' "$OMNIGENT_GITHUB_SESSION_TOKEN"
    ;;
  store|erase)
    exit 0
    ;;
esac
""",
            encoding="utf-8",
        )
        os.chmod(helper, 0o700)

        gh_wrapper = bin_dir / "gh"
        gh_wrapper.write_text(
            """#!/bin/sh
set -eu
proxy=${OMNIGENT_GITHUB_SESSION_PROXY_URL:?}
bundle=${OMNIGENT_GITHUB_SESSION_CA_BUNDLE:?}
export HTTP_PROXY="$proxy" HTTPS_PROXY="$proxy"
export http_proxy="$proxy" https_proxy="$proxy"
export SSL_CERT_FILE="$bundle" REQUESTS_CA_BUNDLE="$bundle"
export CURL_CA_BUNDLE="$bundle" GIT_SSL_CAINFO="$bundle"
exec """
            + shlex.quote(gh)
            + " \"$@\"\n",
            encoding="utf-8",
        )
        os.chmod(gh_wrapper, 0o700)

    def _build_runner_env(self, parent_env: Mapping[str, str]) -> dict[str, str]:
        """Build the runner's opaque and synthetic session environment."""
        bin_dir = self._root / "bin"
        path = parent_env.get("PATH", "")
        return {
            GITHUB_SESSION_ACTIVE_ENV: "1",
            GITHUB_SESSION_ROOT_ENV: str(self._root),
            GITHUB_SESSION_PROXY_ENV: (
                f"http://omnigent:{self._proxy.auth_token}@127.0.0.1:{self._proxy.relay_port}"
            ),
            GITHUB_SESSION_CA_ENV: str(self._proxy.ca_bundle_path),
            GITHUB_SESSION_GIT_CONFIG_ENV: str(self._root / "gitconfig"),
            GITHUB_SESSION_GH_CONFIG_ENV: str(self._root / "gh-config"),
            GITHUB_SESSION_TOKEN_ENV: self._synthetic,
            GITHUB_SESSION_BIN_ENV: str(bin_dir),
            # These are safe session values, not host credentials. Keeping
            # them on the runner itself means direct/native coding-agent
            # subprocesses get ordinary Git/gh behavior too; filtered shell
            # and helper paths rebuild the same values with their own
            # allowlist boundary.
            "GH_TOKEN": self._synthetic,
            "GITHUB_TOKEN": self._synthetic,
            "GH_CONFIG_DIR": str(self._root / "gh-config"),
            "GIT_CONFIG_GLOBAL": str(self._root / "gitconfig"),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_TERMINAL_PROMPT": "0",
            "PATH": f"{bin_dir}{os.pathsep}{path}" if path else str(bin_dir),
        }

    def runner_env(self) -> dict[str, str]:
        """Return a copy of the non-secret environment handed to the runner."""
        return dict(self._runner_env)

    def wrap_runner_command(self, argv: Sequence[str]) -> list[str]:
        """Run the external runner with host credential directories masked."""
        # This is a trusted-host containment layer, not the managed sandbox's
        # policy backend. Still give the runner a private PID view: without
        # it, a same-UID process could inspect the host daemon through /proc
        # and use its root mount namespace to walk around these masks.
        command = [
            self._bwrap,
            "--die-with-parent",
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--new-session",
            "--bind",
            "/",
            "/",
            "--dev",
            "/dev",
            "--proc",
            "/proc",
        ]
        for path in self._masked_paths:
            if path.is_dir():
                command.extend(("--tmpfs", str(path)))
            elif path.exists():
                command.extend(("--bind", "/dev/null", str(path)))
        command.extend(("--", *argv))
        return command

    def close(self) -> None:
        """Stop the proxy, drop the real token, and remove session assets."""
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._proxy.stop()
        self._provider.close()
        self._runner_env.clear()
        cleanup_private_tmpdir(self._root)

    def __del__(self) -> None:
        with contextlib.suppress(Exception):
            self.close()


def _git_config_value(value: str) -> str:
    """Quote a host Git identity safely for the session config."""
    if any(char in value for char in "\r\n"):
        return '""'
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _read_global_git_value(key: str) -> str | None:
    """Read one non-secret identity field without changing host config."""
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--get", key],
            capture_output=True,
            text=True,
            timeout=_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    value = (result.stdout or "").strip()
    return value or None


def _read_git_request() -> dict[str, str]:
    """Parse git's ``key=value`` credential request from stdin (blank-line terminated)."""
    fields: dict[str, str] = {}
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            break
        key, _, value = line.partition("=")
        fields[key] = value
    return fields


def _credential_url(server: str, host_id: str) -> str:
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/github"


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    """Fetch the credential endpoint JSON, or ``None`` on any failure."""
    try:
        resp = httpx.get(
            _credential_url(server, host_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        data = resp.json()
    except ValueError:
        return None
    # Guard non-object JSON (a top-level list/string) so callers' ``data.get(...)``
    # can't raise — keeps the broker fetch's "never raises" contract honest.
    return data if isinstance(data, dict) else None


def _fetch_credential(server: str, host_id: str, host_token: str) -> tuple[str, str] | None:
    """Fetch ``(username, token)`` from the server, or ``None`` if unavailable."""
    data = _fetch(server, host_id, host_token)
    if not data or not data.get("connected") or not data.get("token"):
        return None
    return str(data.get("username") or "x-access-token"), str(data["token"])


def _git_config(*args: str) -> None:
    """Run ``git config --global`` best-effort (never raises; git may be absent)."""
    with contextlib.suppress(OSError, subprocess.SubprocessError):
        subprocess.run(
            ["git", "config", "--global", *args],
            check=False,
            capture_output=True,
            timeout=_TIMEOUT_S,
        )


def _install_broker_helper(server_url: str, host_id: str, token: str) -> None:
    """Make the per-user broker the sole github.com credential helper.

    Bakes the (lesser, expiring) launch token into the helper invocation so it
    works regardless of whether the agent's process inherited the env; the GitHub
    token itself is fetched per-op and never stored. Resets the github.com helper
    chain first: git accumulates credential helpers across scopes and runs them in
    parse order (system → global), stopping at the first that returns a full
    credential. A managed image installs a wider-scope helper that answers
    github.com from a shared ``$GIT_TOKEN``; parsed before this --global entry it
    would vend first and silently bypass the per-user broker. An empty value
    clears the inherited chain for github.com, so only the broker remains.

    Idempotent by ``--replace-all``: the workspace-prep init container and the
    host both wire the broker into the same shared ~/.gitconfig (and the host
    re-runs on every resume). A plain set can't overwrite the 2+ values that
    leaves, so without ``--replace-all`` broker entries would pile up.
    """
    helper = (
        "!python3 -m omnigent.git_credential_github "
        f"--server {shlex.quote(server_url)} --host-id {shlex.quote(host_id)} "
        f"--host-token {shlex.quote(token)}"
    )
    _git_config("--replace-all", "credential.https://github.com.helper", "")
    _git_config("--add", "credential.https://github.com.helper", helper)


def configure_host_git(server_url: str, host_id: str) -> None:
    """Configure git in a managed sandbox to use the GitHub credential broker.

    Called by ``omnigent host`` at startup — executor-agnostic, since the host
    runs in every executor and holds ``$OMNIGENT_HOST_TOKEN``. When the owner has
    GitHub connected (the per-user Connect model), makes the broker the
    authoritative github.com ``credential.helper`` (fetching the owner's token
    from the server per git op; never written to disk) and sets the commit author
    to them. Best-effort: never raises (git may be absent, or GitHub not
    configured on the server).

    Connected-gated, mirroring :func:`configure_clone_credentials`: a confirmed
    ``connected: false`` means the owner hasn't linked GitHub (a shared-
    ``$GIT_TOKEN`` or local deployment). Installing the broker would reset the
    github.com chain (clearing a shared ``$GIT_TOKEN`` helper) and then decline,
    breaking in-session git — so that path instead **clears** any broker helper a
    prior (inconclusive) clone probe may have installed, restoring the ambient
    helper. Only a connected owner, or an inconclusive probe (``None``,
    fail-closed like the clone), takes over github.com.

    Sandbox-only (see :func:`_in_sandbox`): a no-op outside a managed sandbox, so
    a local ``omnigent host`` never rewrites the developer's real ``~/.gitconfig``.
    """
    if not _in_sandbox():
        return
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return
    data = _fetch(server_url, host_id, token)
    if data is not None and not data.get("connected"):
        # Confirmed not linked: actively clear any broker helper a prior
        # (inconclusive) clone probe installed. Just returning would leave that
        # helper — and the chain reset it wrote — in place, stranding in-session
        # git behind a broker that now declines. Unsetting restores the ambient
        # shared-``$GIT_TOKEN`` helper.
        _git_config("--unset-all", "credential.https://github.com.helper")
        return
    _install_broker_helper(server_url, host_id, token)
    owner = str((data or {}).get("owner") or "")
    if data and data.get("connected") and "@" in owner:
        _git_config("user.email", owner)
        login = data.get("login")
        _git_config("user.name", str(login) if login else owner.split("@", 1)[0])


def _gh_config_dir() -> Path:
    """The gh CLI config dir (``GH_CONFIG_DIR`` override, else ``~/.config/gh``)."""
    override = (os.environ.get("GH_CONFIG_DIR") or "").strip()
    if override:
        return Path(override)
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "gh"


def _write_gh_hosts(login: str, token: str) -> bool:
    """Materialize the owner's ``github.com`` auth into gh's ``hosts.yml``.

    The gh CLI does not consult git's ``credential.helper`` for its own API
    calls (``gh api`` / ``gh pr`` / ``gh issue``); it reads ``oauth_token`` from
    ``hosts.yml`` (or ``GH_TOKEN``).

    Writes **only the** ``github.com`` **key, merging** into any existing
    document so a user's other hosts survive (``hosts.yml`` is a multi-host map —
    a GitHub Enterprise entry, a second account). This matters because the host's
    ``GH_CONFIG_DIR`` is not always a throwaway sandbox one: a local ``omnigent
    host`` resolves it to the real ``~/.config/gh``, where truncating would
    destroy the developer's config on every refresh. Mirrors the Databricks
    config writer in :mod:`omnigent.onboarding.setup`, which mutates one section
    of ``~/.databrickscfg`` and writes the rest back rather than truncating.

    Written via a fresh ``0600`` temp file + :func:`os.replace`, so the token is
    never briefly world-readable and a concurrent ``gh`` read never sees a
    partial file; :func:`yaml.safe_dump` quotes values correctly whatever they
    contain.

    NB deliberately no ``gh auth setup-git``: that would register gh as a git
    credential helper and compete with the per-user broker, which must stay
    authoritative for github.com so git ops fetch the token fresh per op.

    :returns: ``True`` when written; ``False`` on any filesystem error.
    """
    hosts_path = _gh_config_dir() / "hosts.yml"
    # Merge into the existing document so other hosts / accounts are preserved. A
    # missing, unreadable, or non-mapping file is treated as empty (never a
    # failure) — the worst case is starting a fresh map.
    hosts: dict = {}
    with contextlib.suppress(OSError, yaml.YAMLError):
        loaded = yaml.safe_load(hosts_path.read_text(encoding="utf-8"))
        if isinstance(loaded, dict):
            hosts = loaded
    entry = hosts.get("github.com")
    if not isinstance(entry, dict):
        entry = {}
    entry.update({"oauth_token": token, "user": login, "git_protocol": "https"})
    hosts["github.com"] = entry

    tmp: str | None = None
    try:
        hosts_path.parent.mkdir(parents=True, exist_ok=True)
        # mkstemp creates the file 0600 (owner-only, umask-independent), so the
        # credential never has a world-readable window; os.replace swaps it in
        # atomically, so a concurrent gh read never sees a partial file.
        fd, tmp = tempfile.mkstemp(dir=hosts_path.parent, prefix=".hosts.", suffix=".yml")
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(hosts, handle, default_flow_style=False, sort_keys=True)
        os.replace(tmp, hosts_path)
    except (OSError, yaml.YAMLError):
        if tmp is not None:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        return False
    return True


def configure_host_gh(server_url: str, host_id: str) -> bool:
    """Authenticate the sandbox's gh CLI as the session owner.

    Companion to :func:`configure_host_git` (which wires git's per-op broker):
    the gh CLI has no per-op credential hook, so — like the Databricks config
    writer in :mod:`omnigent.onboarding.setup` materializes ``~/.databrickscfg``
    — the owner's brokered GitHub token is materialized into ``hosts.yml`` at
    host startup. The token is a short-lived, server-refreshed user token; it is
    re-fetched on each host launch, so a long-lived session should relaunch to
    refresh it.

    Best-effort: a no-op when the host token is absent, the broker is
    unreachable, or the owner hasn't linked GitHub (any ambient gh auth is left
    untouched). Never raises.

    :returns: ``True`` when gh was authenticated; ``False`` otherwise.
    """
    if not _in_sandbox():
        return False
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if not data or not data.get("connected") or not data.get("token"):
        return False
    gh_token = str(data["token"])
    login = str(data.get("login") or data.get("username") or "x-access-token")
    return _write_gh_hosts(login, gh_token)


# How often the background refresher re-materializes gh's hosts.yml, in seconds.
# NB the name deliberately avoids a TOKEN/KEY/SECRET/PASSWORD/CREDENTIAL segment:
# the managed-sandbox launcher rejects env-passthrough names that look like a
# credential (they would land in the Pod spec/etcd), and this is a plain integer.
_GH_REFRESH_INTERVAL_ENV_VAR: str = "OMNIGENT_GH_REFRESH_INTERVAL_S"
_GH_REFRESH_DEFAULT_S: int = 1800


def _gh_refresh_interval_s() -> int:
    """Resolve the gh-token refresh interval (env override, else 30 min).

    Non-positive disables the refresher.
    """
    raw = (os.environ.get(_GH_REFRESH_INTERVAL_ENV_VAR) or "").strip()
    if not raw:
        return _GH_REFRESH_DEFAULT_S
    try:
        return int(raw)
    except ValueError:
        return _GH_REFRESH_DEFAULT_S


def start_host_gh_refresh(server_url: str, host_id: str) -> threading.Thread | None:
    """Keep the gh CLI's ``hosts.yml`` token fresh over a long-lived host.

    Unlike git (whose per-op broker helper re-fetches the server-refreshed
    token on every operation), the gh CLI reads a **static** ``hosts.yml``, so
    the token :func:`configure_host_gh` writes at startup goes stale when the
    GitHub App user token expires (~8h) and the server rotates it — a
    long-running session would then hit ``gh api`` 401s. This best-effort daemon
    thread re-materializes ``hosts.yml`` on an interval well under the token
    lifetime, so gh stays authenticated for the life of the host. (An
    agent-sandbox resume already refreshes it by re-running host startup; this
    covers a session that stays live without ever suspending.)

    :returns: The started daemon thread, or ``None`` when disabled (a
        non-positive interval, or outside a managed sandbox) — mainly for tests.
    """
    if not _in_sandbox():
        return None
    interval = _gh_refresh_interval_s()
    if interval <= 0:
        return None

    def _loop() -> None:
        while True:
            time.sleep(interval)
            with contextlib.suppress(Exception):
                configure_host_gh(server_url, host_id)

    thread = threading.Thread(target=_loop, name="gh-token-refresh", daemon=True)
    thread.start()
    return thread


def configure_clone_credentials(server_url: str, host_id: str) -> bool:
    """Wire the per-user broker for the initial workspace clone.

    Called by the managed-sandbox ``workspace-prep`` init container BEFORE it
    clones the workspace repo. Like :func:`configure_host_git`, this is
    connected-gated: it keeps the ambient credential chain — notably the image's
    shared ``$GIT_TOKEN`` helper — when the owner is *definitively* not linked, so
    a shared-token clone still works for them.

    Fails closed on an ambiguous probe: :func:`_fetch` returns ``None`` on any
    transient fault (timeout, non-200, bad JSON), which is indistinguishable from
    "not linked". Treating that as unlinked would silently clone a *linked*
    owner's private repo under the shared image identity, defeating the per-user
    contract. So only a **successful** ``connected: false`` keeps the shared
    fallback; a connected owner — or an unresolved probe — installs the broker,
    so the clone authenticates per-user or fails visibly instead of quietly
    falling back to the shared token. Best-effort: never raises.

    :returns: ``True`` when the broker was wired (owner connected, or the probe
        was inconclusive); ``False`` only when the owner is confirmed not linked.
    """
    token = (os.environ.get(_HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    data = _fetch(server_url, host_id, token)
    if data is not None and not data.get("connected"):
        return False
    _install_broker_helper(server_url, host_id, token)
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--server", required=True)
    parser.add_argument("--host-id", required=True)
    parser.add_argument("--host-token", required=True)
    # git passes the operation (get/store/erase) as the first positional arg.
    parser.add_argument("operation", nargs="?", default="get")
    args, _ = parser.parse_known_args(argv)

    # Only ``get`` returns credentials; ``store``/``erase`` are no-ops (nothing
    # is persisted — the token is re-fetched next time).
    if args.operation != "get":
        return 0

    request = _read_git_request()
    # Fail closed: only vend for github.com over https. A missing/empty host, a
    # different host, or a non-https protocol declines (return 0, no output) so
    # git falls through to the next helper — the brokered token can never leak
    # to another host even if this is ever wired as a global credential helper.
    if request.get("host") != "github.com" or request.get("protocol") != "https":
        return 0

    cred = _fetch_credential(args.server, args.host_id, args.host_token)
    if cred is None:
        return 0  # decline; git tries the next helper / prompts
    username, token = cred
    sys.stdout.write(f"username={username}\npassword={token}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
