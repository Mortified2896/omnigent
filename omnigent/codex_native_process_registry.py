"""Crash-safe process registry for native Codex app-server children."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import signal
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from omnigent.codex_native_state import _codex_native_state_root

_logger = logging.getLogger(__name__)
_REGISTRY_FILE = "process-registry.json"
_TAG_ARG_PREFIX = "omnigent_crash_teardown_tag="


@dataclass(frozen=True)
class CodexNativeProcessEntry:
    """
    One crash-reapable native Codex subprocess registry entry.

    :param pid: Child process id.
    :param pgid: Child process group id.
    :param tmux_session_name: Optional tmux session name owned by the child.
    :param session_tag: Unique tag also embedded in the child command line.
    """

    pid: int
    pgid: int
    tmux_session_name: str | None
    session_tag: str


def codex_native_process_registry_path() -> Path:
    """
    Return the stable on-disk registry file path.

    :returns: Registry path under the existing codex-native state root.
    """
    return _codex_native_state_root() / _REGISTRY_FILE


def codex_native_session_tag_cmdline_arg(session_tag: str) -> str:
    """
    Return an inert command-line marker carrying the crash-reap tag.

    :param session_tag: Unique per-process tag.
    :returns: Command-line marker value.
    :raises ValueError: If *session_tag* is empty.
    """
    if not session_tag:
        raise ValueError("session_tag must be non-empty")
    return f"{_TAG_ARG_PREFIX}{session_tag}"


def register_codex_native_process(
    *,
    pid: int,
    pgid: int,
    session_tag: str,
    tmux_session_name: str | None = None,
    registry_path: Path | None = None,
) -> None:
    """
    Add or replace one native Codex process registry entry.

    :param pid: Child process id.
    :param pgid: Child process group id.
    :param session_tag: Unique tag embedded in the child command line.
    :param tmux_session_name: Optional tmux session name owned by the child.
    :param registry_path: Test override for the registry file path.
    :returns: None.
    """
    if pid <= 0 or pgid <= 0 or not session_tag:
        return
    entry = CodexNativeProcessEntry(
        pid=pid,
        pgid=pgid,
        tmux_session_name=tmux_session_name,
        session_tag=session_tag,
    )
    path = registry_path or codex_native_process_registry_path()
    entries = [existing for existing in _read_registry(path) if existing.session_tag != session_tag]
    entries.append(entry)
    _write_registry(path, entries)


def unregister_codex_native_process(
    session_tag: str,
    *,
    registry_path: Path | None = None,
) -> None:
    """
    Remove one native Codex process registry entry.

    :param session_tag: Unique per-process registry tag.
    :param registry_path: Test override for the registry file path.
    :returns: None.
    """
    if not session_tag:
        return
    path = registry_path or codex_native_process_registry_path()
    entries = [entry for entry in _read_registry(path) if entry.session_tag != session_tag]
    _write_registry(path, entries)


def reconcile_codex_native_process_registry(
    *, registry_path: Path | None = None
) -> None:
    """
    Reap crash-leftover native Codex children recorded by prior runs.

    PID reuse is guarded by requiring the live process command line to
    still contain the entry's unique session tag before killing anything.

    :param registry_path: Test override for the registry file path.
    :returns: None.
    """
    path = registry_path or codex_native_process_registry_path()
    survivors: list[CodexNativeProcessEntry] = []
    for entry in _read_registry(path):
        if not _pid_alive(entry.pid):
            continue
        if not _process_cmdline_has_tag(entry.pid, entry.session_tag):
            continue
        if not _terminate_process_group(entry.pgid):
            survivors.append(entry)
            continue
        _reap_tmux_session(entry.tmux_session_name)
    _write_registry(path, survivors)


def _read_registry(path: Path) -> list[CodexNativeProcessEntry]:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except OSError:
        _logger.warning("codex-native process registry read failed", exc_info=True)
        return []
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        _logger.warning("codex-native process registry JSON is malformed; ignoring")
        return []
    if not isinstance(payload, list):
        return []
    entries: list[CodexNativeProcessEntry] = []
    for item in payload:
        entry = _entry_from_json(item)
        if entry is not None:
            entries.append(entry)
    return entries


def _write_registry(path: Path, entries: list[CodexNativeProcessEntry]) -> None:
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        payload = [asdict(entry) for entry in entries]
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, separators=(",", ":")) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        _logger.warning("codex-native process registry write failed", exc_info=True)


def _entry_from_json(item: Any) -> CodexNativeProcessEntry | None:
    if not isinstance(item, dict):
        return None
    pid = item.get("pid")
    pgid = item.get("pgid")
    session_tag = item.get("session_tag")
    tmux_session_name = item.get("tmux_session_name")
    if not isinstance(pid, int) or pid <= 0:
        return None
    if not isinstance(pgid, int) or pgid <= 0:
        return None
    if not isinstance(session_tag, str) or not session_tag:
        return None
    if tmux_session_name is not None and not isinstance(tmux_session_name, str):
        tmux_session_name = None
    return CodexNativeProcessEntry(
        pid=pid,
        pgid=pgid,
        tmux_session_name=tmux_session_name,
        session_tag=session_tag,
    )


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _process_cmdline_has_tag(pid: int, session_tag: str) -> bool:
    needle = codex_native_session_tag_cmdline_arg(session_tag)
    cmdline = _process_cmdline(pid)
    return needle in cmdline


def _process_cmdline(pid: int) -> str:
    proc_cmdline = Path("/proc") / str(pid) / "cmdline"
    with contextlib.suppress(OSError):
        raw = proc_cmdline.read_bytes()
        if raw:
            return raw.replace(b"\x00", b" ").decode("utf-8", errors="replace")
    try:
        proc = subprocess.run(
            ["ps", "-p", str(pid), "-ww", "-o", "command="],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _terminate_process_group(pgid: int) -> bool:
    if os.name == "posix":
        try:
            os.killpg(pgid, signal.SIGTERM)
        except ProcessLookupError:
            return True
        except (PermissionError, OSError):
            return False
        return True
    return False


def _reap_tmux_session(tmux_session_name: str | None) -> None:
    if not tmux_session_name:
        return
    if _tmux_session_exists(tmux_session_name):
        _kill_tmux_session(tmux_session_name)


def _tmux_session_exists(tmux_session_name: str) -> bool:
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", tmux_session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _kill_tmux_session(tmux_session_name: str) -> None:
    with contextlib.suppress(OSError, subprocess.TimeoutExpired):
        subprocess.run(
            ["tmux", "kill-session", "-t", tmux_session_name],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.0,
        )
