"""Actual private-home merge plus independently discovered project hooks."""

from __future__ import annotations

import hashlib
import json
import shlex
from pathlib import Path
from typing import Any

import pytest

from omnigent.codex_native_app_server import _write_codex_policy_hooks_file
from omnigent.inner.codex_hook_ownership import (
    inherited_provenance_replacement,
    reconcile_inherited_provenance_hooks,
)

_EVENTS = {
    "PreToolUse": ("preToolUse", "pre_tool_use"),
    "PostToolUse": ("postToolUse", "post_tool_use"),
    "UserPromptSubmit": ("userPromptSubmit", "user_prompt_submit"),
    "Stop": ("stop", "stop"),
}


def _fixture(tmp_path: Path) -> tuple[Path, Path, str]:
    source = tmp_path / "user" / ".codex" / "hooks.json"
    source.parent.mkdir(parents=True)
    script = (
        Path.home()
        / "Library/Application Support/Codex/TelemetryProvenance/bin/codex_otel_decisions.py"
    )
    command = shlex.join(["/usr/bin/python3", str(script), "hook"])
    hooks = {
        event: [{"hooks": [{"type": "command", "command": command, "timeout": 120}]}]
        for event in ("PreToolUse", "UserPromptSubmit", "Stop")
    }
    hooks["PreToolUse"][0]["matcher"] = ".*"
    # This identical unrelated command must survive in both source layers.
    hooks["Stop"].append({"hooks": [{"type": "command", "command": "echo user-stop"}]})
    source.write_text(json.dumps({"hooks": hooks}))
    private = tmp_path / "private"
    _write_codex_policy_hooks_file(
        private,
        tmp_path / "bridge",
        "/python",
        user_hooks_source=source,
        router_bridge_dir=tmp_path / "router",
        router_session_id="session",
        turn_routing=True,
    )
    return private, source, str(source.parent.parent)


def _listed(private: Path, source: Path, cwd: str) -> dict[str, Any]:
    handlers = []
    for path, layer in ((private / "hooks.json", "user"), (source, "project")):
        for event, groups in json.loads(path.read_text())["hooks"].items():
            api_event, key_event = _EVENTS[event]
            for i, group in enumerate(groups):
                for j, handler in enumerate(group["hooks"]):
                    handlers.append(
                        {
                            "key": f"{path}:{key_event}:{i}:{j}",
                            "sourcePath": str(path),
                            "source": layer,
                            "eventName": api_event,
                            "handlerType": "command",
                            "command": handler["command"],
                            "enabled": True,
                            "isManaged": False,
                            "trustStatus": "trusted",
                            "matcher": group.get("matcher"),
                            "currentHash": "sha256:"
                            + hashlib.sha256(
                                json.dumps(
                                    [event, group.get("matcher"), handler], sort_keys=True
                                ).encode()
                            ).hexdigest(),
                            "displayOrder": len(handlers),
                        }
                    )
    return {"result": {"data": [{"cwd": cwd, "hooks": handlers, "warnings": [], "errors": []}]}}


async def test_live_layer_merge_removes_only_provenance_and_preserves_trust_keys(
    tmp_path: Path,
) -> None:
    private, source, cwd = _fixture(tmp_path)
    before_source = source.read_bytes()
    before = _listed(private, source, cwd)
    calls = []

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        calls.append((method, params))
        return _listed(private, source, cwd) if method == "hooks/list" else {"result": {}}

    removed = await reconcile_inherited_provenance_hooks(
        request, cwd=cwd, codex_home=private, source_path=source
    )
    assert len(removed) == 3
    assert source.read_bytes() == before_source
    after = _listed(private, source, cwd)
    old = {h["key"]: h for h in before["result"]["data"][0]["hooks"]}
    new = {h["key"]: h for h in after["result"]["data"][0]["hooks"]}
    assert set(new) == set(old) - set(removed)
    for key, handler in new.items():
        assert {k: v for k, v in handler.items() if k != "displayOrder"} == {
            k: v for k, v in old[key].items() if k != "displayOrder"
        }
    assert sum(h["command"] == "echo user-stop" for h in new.values()) == 2
    assert any("route-turn" in h["command"] for h in new.values())
    assert any("route-subagent" in h["command"] for h in new.values())
    assert (
        "config/batchWrite",
        {"edits": [], "filePath": str(private / "config.toml"), "reloadUserConfig": True},
    ) in calls
    calls.clear()
    assert (
        await reconcile_inherited_provenance_hooks(
            request, cwd=cwd, codex_home=private, source_path=source
        )
        == []
    )
    assert all(method != "config/batchWrite" for method, _ in calls)


@pytest.mark.parametrize(
    "change",
    [
        "absent",
        "untrusted",
        "disabled",
        "different_hash",
        "plugin",
        "different_path",
        "wrong_cwd",
        "errors",
    ],
)
def test_keeps_private_capture_without_a_verified_source(tmp_path: Path, change: str) -> None:
    private, source, cwd = _fixture(tmp_path)
    listed = _listed(private, source, cwd)
    entry = listed["result"]["data"][0]
    for h in entry["hooks"]:
        if h["source"] != "project":
            continue
        if change == "untrusted":
            h["trustStatus"] = "untrusted"
        if change == "disabled":
            h["enabled"] = False
        if change == "different_hash":
            h["currentHash"] = "sha256:changed"
        if change == "plugin":
            h["source"] = "plugin"
        if change == "different_path":
            h["sourcePath"] = "/another/hooks.json"
    if change == "absent":
        entry["hooks"] = [h for h in entry["hooks"] if h["source"] != "project"]
    if change == "wrong_cwd":
        entry["cwd"] = "/other"
    if change == "errors":
        entry["errors"] = ["invalid source"]
    original = (private / "hooks.json").read_bytes()
    assert inherited_provenance_replacement(
        original, listed, cwd=cwd, private_path=private / "hooks.json", source_path=source
    ) == (original, [])


async def test_reload_failure_restores_private_file(tmp_path: Path) -> None:
    private, source, cwd = _fixture(tmp_path)
    original = (private / "hooks.json").read_bytes()
    reloads = 0

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal reloads
        if method == "hooks/list":
            return _listed(private, source, cwd)
        reloads += 1
        if reloads == 1:
            raise RuntimeError("reload failed")
        return {"result": {}}

    with pytest.raises(RuntimeError, match="reload failed"):
        await reconcile_inherited_provenance_hooks(
            request, cwd=cwd, codex_home=private, source_path=source
        )
    assert reloads == 2
    assert (private / "hooks.json").read_bytes() == original


async def test_dropped_unrelated_hook_restores_private_file(tmp_path: Path) -> None:
    private, source, cwd = _fixture(tmp_path)
    original = (private / "hooks.json").read_bytes()
    reads = 0

    async def request(method: str, params: dict[str, object]) -> dict[str, object]:
        nonlocal reads
        if method != "hooks/list":
            return {"result": {}}
        reads += 1
        result = _listed(private, source, cwd)
        if reads == 2:
            result["result"]["data"][0]["hooks"].pop()
        return result

    with pytest.raises(RuntimeError, match="changed other hooks"):
        await reconcile_inherited_provenance_hooks(
            request, cwd=cwd, codex_home=private, source_path=source
        )
    assert (private / "hooks.json").read_bytes() == original


def test_mixed_group_keeps_handler_positions(tmp_path: Path) -> None:
    private, source, cwd = _fixture(tmp_path)
    path = private / "hooks.json"
    payload = json.loads(path.read_text())
    payload["hooks"]["Stop"][0]["hooks"].append(
        {"type": "command", "command": "echo keep-position"}
    )
    path.write_text(json.dumps(payload))
    original = path.read_bytes()
    listed = _listed(private, source, cwd)
    # Give the copied provenance handler the source hash, as Codex hashes each handler.
    hooks = listed["result"]["data"][0]["hooks"]
    source_hook = next(h for h in hooks if h["source"] == "project" and h["eventName"] == "stop")
    private_hook = next(h for h in hooks if h["source"] == "user" and h["eventName"] == "stop")
    private_hook["currentHash"] = source_hook["currentHash"]
    replacement, removed = inherited_provenance_replacement(
        original, listed, cwd=cwd, private_path=path, source_path=source
    )
    assert len(removed) == 2
    assert json.loads(replacement)["hooks"]["Stop"] == payload["hooks"]["Stop"]
