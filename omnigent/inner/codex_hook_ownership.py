"""Keep copied Mac provenance hooks from running through two active layers."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path

from omnigent.json_types import JsonObject

_EVENTS = {
    "preToolUse": ("PreToolUse", "pre_tool_use"),
    "userPromptSubmit": ("UserPromptSubmit", "user_prompt_submit"),
    "stop": ("Stop", "stop"),
}


def _is_provenance_command(command: object) -> bool:
    if not isinstance(command, str):
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    script = (
        Path.home()
        / "Library/Application Support/Codex/TelemetryProvenance/bin/codex_otel_decisions.py"
    )
    return len(argv) == 3 and argv == ["/usr/bin/python3", str(script), "hook"]


def inherited_provenance_replacement(
    original: bytes,
    listed: Mapping[str, object],
    *,
    cwd: str,
    private_path: Path,
    source_path: Path,
) -> tuple[bytes, list[str]]:
    """Plan removal only for exact, independently trusted source registrations.

    Empty matcher groups preserve every remaining handler's positional trust
    key. Mixed groups are left intact when removal would renumber a handler.
    """
    result = listed.get("result", listed)
    if not isinstance(result, Mapping):
        return original, []
    data = result.get("data")
    if not isinstance(data, list) or private_path == source_path:
        return original, []
    entries = [entry for entry in data if isinstance(entry, dict) and entry.get("cwd") == cwd]
    if len(entries) != 1 or entries[0].get("errors") or entries[0].get("warnings"):
        return original, []
    hooks = entries[0].get("hooks")
    if not isinstance(hooks, list):
        return original, []
    handlers = [
        h for h in hooks if isinstance(h, dict) and _is_provenance_command(h.get("command"))
    ]
    inherited = {
        (h.get("eventName"), h.get("command"), h.get("currentHash"))
        for h in handlers
        if h.get("sourcePath") == str(source_path)
        and h.get("source") == "project"
        and h.get("handlerType") == "command"
        and h.get("enabled") is True
        and h.get("trustStatus") in {"trusted", "managed"}
        and isinstance(h.get("currentHash"), str)
        and h["currentHash"].startswith("sha256:")
        and isinstance(h.get("key"), str)
        and h["key"].startswith(f"{source_path}:")
    }
    if not inherited:
        return original, []
    payload = json.loads(original)
    removed = []
    for handler in handlers:
        event = handler.get("eventName")
        key = handler.get("key")
        if (
            event not in _EVENTS
            or handler.get("sourcePath") != str(private_path)
            or handler.get("source") != "user"
            or handler.get("handlerType") != "command"
            or handler.get("enabled") is not True
            or handler.get("isManaged") is not False
            or not isinstance(key, str)
            or (event, handler.get("command"), handler.get("currentHash")) not in inherited
        ):
            continue
        json_event, key_event = _EVENTS[event]
        prefix = f"{private_path}:{key_event}:"
        if not key.startswith(prefix):
            continue
        try:
            group_index, hook_index = map(int, key.removeprefix(prefix).split(":"))
            if min(group_index, hook_index) < 0:
                continue
            group = payload["hooks"][json_event][group_index]
            group_hooks = group["hooks"]
            if (
                hook_index != len(group_hooks) - 1
                or group_hooks[hook_index].get("type") != "command"
                or group_hooks[hook_index].get("command") != handler["command"]
            ):
                continue
        except (KeyError, IndexError, TypeError, ValueError):
            continue
        group_hooks.pop()
        removed.append(key)
    if not removed:
        return original, []
    return (json.dumps(payload, sort_keys=True) + "\n").encode(), removed


async def reconcile_inherited_provenance_hooks(
    request: Callable[[str, JsonObject], Awaitable[JsonObject]],
    *,
    cwd: str,
    codex_home: Path,
    source_path: Path,
) -> list[str]:
    """Reconcile the generated private file, then reload through Codex's API.

    The source hooks, trust state, and policy/routing handlers remain owned by
    their existing writers. A reload failure restores the original private file.
    """
    path = codex_home / "hooks.json"
    if path.is_symlink() or not path.is_file():
        return []
    original = path.read_bytes()
    listed = await request("hooks/list", {"cwds": [cwd]})
    replacement, removed = inherited_provenance_replacement(
        original, listed, cwd=cwd, private_path=path, source_path=source_path
    )
    if not removed:
        return []

    def replace(expected: bytes, content: bytes) -> None:
        if path.is_symlink() or path.read_bytes() != expected:
            raise RuntimeError("private hooks changed during provenance ownership reconciliation")
        fd, name = tempfile.mkstemp(prefix="hooks.json.", dir=codex_home)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)

    params: JsonObject = {
        "edits": [],
        "filePath": str(codex_home / "config.toml"),
        "reloadUserConfig": True,
    }
    replace(original, replacement)
    try:
        await request("config/batchWrite", params)
        relisted = await request("hooks/list", {"cwds": [cwd]})
        result = relisted.get("result", relisted)
        if not isinstance(result, dict):
            raise RuntimeError("Codex did not confirm the reconciled provenance hooks")
        data = result.get("data")
        if not isinstance(data, list):
            raise RuntimeError("Codex did not confirm the reconciled provenance hooks")
        current = [entry for entry in data if isinstance(entry, dict) and entry.get("cwd") == cwd]
        if (
            len(current) != 1
            or current[0].get("errors")
            or current[0].get("warnings")
            or not isinstance(current[0].get("hooks"), list)
            or any(h.get("key") in removed for h in current[0]["hooks"] if isinstance(h, dict))
        ):
            raise RuntimeError("Codex still discovers the redundant provenance hooks")
        before_result = listed.get("result", listed)
        assert isinstance(before_result, dict)
        before_data = before_result["data"]
        assert isinstance(before_data, list)
        before_entry = next(e for e in before_data if isinstance(e, dict) and e.get("cwd") == cwd)

        def retained(handlers: object) -> dict[str, object]:
            assert isinstance(handlers, list)
            return {
                h["key"]: {k: v for k, v in h.items() if k != "displayOrder"}
                for h in handlers
                if isinstance(h, dict)
                and isinstance(h.get("key"), str)
                and h["key"] not in removed
            }

        if retained(current[0]["hooks"]) != retained(before_entry["hooks"]):
            raise RuntimeError("Codex changed other hooks during provenance reconciliation")
    except BaseException:
        replace(replacement, original)
        await request("config/batchWrite", params)
        raise
    return removed
