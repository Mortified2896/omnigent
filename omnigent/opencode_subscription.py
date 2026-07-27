"""Safe discovery of ChatGPT OAuth models managed by OpenCode."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

_OPENAI_SDK = "@ai-sdk/openai"
_OPENAI_DOC_HOST = "platform.openai.com"
_AUTH_RELATIVE_PATH = Path("opencode") / "auth.json"


@dataclass(frozen=True)
class OpenCodeOAuthState:
    """Sanitized location and provider identity for a usable OAuth login."""

    auth_path: Path
    xdg_data_home: Path
    xdg_config_home: Path
    provider_id: str


def _read_object(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("JSON root is not an object")
    return value


def _candidate_auth_paths(*, home: Path, bridge_root: Path) -> list[Path]:
    xdg_data = os.environ.get("XDG_DATA_HOME", "").strip()
    user_data = Path(xdg_data) if xdg_data else home / ".local" / "share"
    paths = [user_data / _AUTH_RELATIVE_PATH]
    if bridge_root.is_dir():
        paths.extend(bridge_root.glob("*/xdg-data/opencode/auth.json"))
    return list(dict.fromkeys(paths))


def _xdg_roots(auth_path: Path, *, home: Path, bridge_root: Path) -> tuple[Path, Path]:
    data_home = auth_path.parent.parent
    try:
        relative = auth_path.relative_to(bridge_root)
    except ValueError:
        xdg_config = os.environ.get("XDG_CONFIG_HOME", "").strip()
        config_home = Path(xdg_config) if xdg_config else home / ".config"
    else:
        config_home = bridge_root / relative.parts[0] / "xdg-config"
    return data_home, config_home


def _chatgpt_oauth_provider(
    auth: dict[str, Any], catalog: dict[str, Any], *, now_ms: int
) -> tuple[str | None, str]:
    saw_matching_oauth = False
    saw_expired = False
    for provider_id, credential in auth.items():
        provider = catalog.get(provider_id)
        doc = provider.get("doc") if isinstance(provider, dict) else None
        if (
            not isinstance(provider_id, str)
            or not isinstance(provider, dict)
            or provider.get("id") != provider_id
            or provider.get("npm") != _OPENAI_SDK
            or not isinstance(doc, str)
            or urlparse(doc).hostname != _OPENAI_DOC_HOST
            or not isinstance(credential, dict)
            or credential.get("type") != "oauth"
        ):
            continue
        saw_matching_oauth = True
        required_fields = ("access", "refresh", "accountId")
        if not all(
            isinstance(credential.get(key), str) and credential[key] for key in required_fields
        ):
            continue
        expires = credential.get("expires")
        if not isinstance(expires, (int, float)) or expires <= now_ms:
            saw_expired = True
            continue
        return provider_id, "available"
    if saw_expired:
        return None, "expired"
    if saw_matching_oauth:
        return None, "malformed"
    return None, "absent"


def chatgpt_provider_env(provider_id: str, *, catalog_path: Path | None = None) -> tuple[str, ...]:
    """Return API-key environment names for OpenCode's built-in ChatGPT provider."""

    resolved_catalog = catalog_path or Path.home() / ".cache" / "opencode" / "models.json"
    try:
        catalog = _read_object(resolved_catalog)
    except (json.JSONDecodeError, OSError, ValueError):
        return ()
    provider = catalog.get(provider_id)
    doc = provider.get("doc") if isinstance(provider, dict) else None
    if (
        not isinstance(provider, dict)
        or provider.get("id") != provider_id
        or provider.get("npm") != _OPENAI_SDK
        or not isinstance(doc, str)
        or urlparse(doc).hostname != _OPENAI_DOC_HOST
    ):
        return ()
    raw_env = provider.get("env")
    return (
        tuple(value for value in raw_env if isinstance(value, str))
        if isinstance(raw_env, list)
        else ()
    )


def chatgpt_oauth_provider_for_auth_path(
    auth_path: Path,
    *,
    catalog_path: Path | None = None,
    now_ms: int | None = None,
) -> tuple[str | None, tuple[str, ...]]:
    """Classify one auth store and return only provider/env identifiers."""

    resolved_catalog = catalog_path or Path.home() / ".cache" / "opencode" / "models.json"
    try:
        auth = _read_object(auth_path)
        catalog = _read_object(resolved_catalog)
    except (json.JSONDecodeError, OSError, ValueError):
        return None, ()
    provider_id, _ = _chatgpt_oauth_provider(
        auth, catalog, now_ms=now_ms if now_ms is not None else int(time.time() * 1000)
    )
    if provider_id is None:
        return None, ()
    return provider_id, chatgpt_provider_env(provider_id, catalog_path=resolved_catalog)


def discover_chatgpt_oauth_state(
    *,
    home: Path | None = None,
    bridge_root: Path | None = None,
    catalog_path: Path | None = None,
    now_ms: int | None = None,
) -> tuple[OpenCodeOAuthState | None, str | None]:
    """Find the newest usable ChatGPT OAuth record without returning secrets."""

    resolved_home = home or Path.home()
    resolved_bridge_root = bridge_root or resolved_home / ".omnigent" / "opencode-native"
    resolved_catalog = catalog_path or resolved_home / ".cache" / "opencode" / "models.json"
    try:
        catalog = _read_object(resolved_catalog)
    except (json.JSONDecodeError, OSError, ValueError):
        return None, "OpenCode model catalog is unavailable."

    current_ms = now_ms if now_ms is not None else int(time.time() * 1000)
    matches: list[tuple[int, OpenCodeOAuthState]] = []
    saw_auth_file = False
    failure_states: set[str] = set()
    for path in _candidate_auth_paths(home=resolved_home, bridge_root=resolved_bridge_root):
        if not path.is_file() or path.is_symlink():
            continue
        saw_auth_file = True
        try:
            auth = _read_object(path)
            modified_ns = path.stat().st_mtime_ns
        except (json.JSONDecodeError, OSError, ValueError):
            continue
        provider_id, state = _chatgpt_oauth_provider(auth, catalog, now_ms=current_ms)
        failure_states.add(state)
        if provider_id is None:
            continue
        data_home, config_home = _xdg_roots(
            path, home=resolved_home, bridge_root=resolved_bridge_root
        )
        matches.append(
            (
                modified_ns,
                OpenCodeOAuthState(
                    auth_path=path,
                    xdg_data_home=data_home,
                    xdg_config_home=config_home,
                    provider_id=provider_id,
                ),
            )
        )

    if matches:
        return max(matches, key=lambda item: item[0])[1], None
    if "expired" in failure_states:
        return None, "OpenCode ChatGPT OAuth credential is expired or unusable."
    if "malformed" in failure_states:
        return None, "OpenCode ChatGPT OAuth credential is malformed or unusable."
    if saw_auth_file:
        return None, "OpenCode ChatGPT OAuth credential is not present."
    return None, "OpenCode authentication is not present."


def _parse_verbose_models(output: str, provider_id: str) -> list[dict[str, Any]]:
    decoder = json.JSONDecoder()
    models: list[dict[str, Any]] = []
    seen: set[str] = set()
    lines = output.splitlines()
    for index, line in enumerate(lines):
        qualified_id = line.strip()
        if not qualified_id.startswith(f"{provider_id}/") or qualified_id in seen:
            continue
        remainder = "\n".join(lines[index + 1 :]).lstrip()
        try:
            metadata, _ = decoder.raw_decode(remainder)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(metadata, dict):
            continue
        model_id = metadata.get("id")
        if (
            metadata.get("providerID") != provider_id
            or not isinstance(model_id, str)
            or qualified_id != f"{provider_id}/{model_id}"
        ):
            continue
        seen.add(qualified_id)
        models.append({"id": qualified_id, "metadata": metadata})
    return models


def read_open_code_oauth_models(
    state: OpenCodeOAuthState,
    *,
    executable: str | None = None,
    timeout: float = 20.0,
) -> tuple[list[dict[str, Any]], str | None]:
    """Read the exact provider catalog that OpenCode exposes for OAuth."""

    command = executable or shutil.which("opencode")
    if not command:
        fallback = Path.home() / ".opencode" / "bin" / "opencode"
        command = str(fallback) if fallback.is_file() else None
    if not command:
        return [], "OpenCode executable is unavailable."

    env = os.environ.copy()
    # A coexisting API key must not influence the subscription catalog.
    env.pop("OPENAI_API_KEY", None)
    env.pop("OPENCODE_CONFIG", None)
    env.pop("OPENCODE_CONFIG_CONTENT", None)
    env["XDG_DATA_HOME"] = str(state.xdg_data_home)
    env["XDG_CONFIG_HOME"] = str(state.xdg_config_home)
    try:
        result = subprocess.run(
            [command, "models", state.provider_id, "--verbose", "--pure"],
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=env,
        )
    except (OSError, subprocess.SubprocessError):
        return [], "OpenCode model catalog could not be read."
    if result.returncode != 0:
        return [], "OpenCode model catalog could not be read."
    models = _parse_verbose_models(result.stdout, state.provider_id)
    if not models:
        return [], "OpenCode exposes no models for the ChatGPT OAuth provider."
    return models, None
