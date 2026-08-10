"""Fail-closed host orchestration for O2-supervised O1 promotion.

This module is intentionally narrow: O1 is the only target, O2 is the only
supervisor, and the accepted 0.9 artifact is fixed. It replaces the v2 shell
state machine for live promotion.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import sqlite3
import subprocess
import time
from pathlib import Path
from typing import Any

from . import eligibility, identity, staging, transaction

TARGET = identity.O1
SUPERVISOR = identity.O2
ACCEPTED_SHA = "541c9a3180b81bfb2fc450b3ef5f8648691b359d"
ACCEPTED_VERSION = "0.9.0.dev0"
OLD_SHA = "e5f4249667a1602916d44ac62d10b921a299f05d"
OLD_VERSION = "0.8.1"
OLD_SCHEMA = "c4d5e6f7a8b9"
NEW_SCHEMA = "f7a8b9c0d1e2"
WHEEL_HASHES = {
    f"omnigent-{ACCEPTED_VERSION}-py3-none-any.whl": "f49fb3f973c1d98be03eaede76e9c7e86acb91064b06494afdf8f7345524a5e9",
    f"omnigent_client-{ACCEPTED_VERSION}-py3-none-any.whl": "555a6286477bd528005478571b24cd2fda5c9da505f0957d606b6182614f9605",
    f"omnigent_ui_sdk-{ACCEPTED_VERSION}-py3-none-any.whl": "e2141bc6af3bee42a85cad1ff48d008d20439ee34cb52956cda1b7fdff1d45a9",
}
TARGET_ROOT = TARGET.deployment_root
TARGET_HOME = identity.HOME_MAPPING[str(TARGET_ROOT)]
TARGET_DB = TARGET_HOME / "chat.db"
TARGET_VENV = TARGET_ROOT / "venv"
SOURCE_RELEASE = SUPERVISOR.deployment_root / "releases" / ACCEPTED_SHA
ARTIFACTS = SOURCE_RELEASE / "artifacts"
TERMINAL_PHASES = {"tx_committed", "rolled_back", "failure"}


class PromotionError(RuntimeError):
    pass


class PromotionInterrupted(PromotionError):
    pass


def _run(argv: list[str], *, check: bool = True, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    p = subprocess.run(argv, cwd="/tmp", env=env, text=True, capture_output=True, check=False)
    if check and p.returncode:
        raise PromotionError(f"command failed rc={p.returncode}: {argv!r}; stderr={p.stderr[-1500:]!r}")
    return p


def _svc(unit: str, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["systemctl", *args, unit], check=check)


def _active(unit: str) -> bool:
    return _svc(unit, "is-active", check=False).stdout.strip() == "active"


def _snapshot_service(unit: str) -> dict[str, str]:
    p = _run(["systemctl", "show", unit, "-p", "MainPID", "-p", "NRestarts", "-p", "ActiveEnterTimestampMonotonic", "-p", "ActiveState"])
    return dict(line.split("=", 1) for line in p.stdout.splitlines() if "=" in line)


def _health(url: str) -> str:
    return _run(["curl", "-fsS", "--max-time", "5", url]).stdout.strip()


def _wait_health(url: str, timeout: int = 90) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if _run(["curl", "-fsS", "--max-time", "3", url], check=False).returncode == 0:
            return
        time.sleep(1)
    raise PromotionError(f"health timeout: {url}")


def _sha(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _db(path: Path) -> tuple[str, str, dict[str, int]]:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as c:
        integrity = str(c.execute("PRAGMA integrity_check").fetchone()[0])
        schema = str(c.execute("SELECT version_num FROM alembic_version").fetchone()[0])
        tables = {str(r[0]) for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        counts = {n: int(c.execute(f'SELECT COUNT(*) FROM "{n}"').fetchone()[0]) for n in ("conversations", "items", "labels") if n in tables}
    return integrity, schema, counts


def _atomic_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _supervisor_guard() -> dict[str, Any]:
    identity.require_distinct(TARGET, SUPERVISOR)
    if not _active(SUPERVISOR.service_unit) or not _active(SUPERVISOR.host_unit):
        raise PromotionError("O2 supervisor services are not both active")
    runtime = identity.runtime_identity(SUPERVISOR.deployment_root / "current" / "venv" / "bin" / "python")
    if runtime.get("commit_sha") != ACCEPTED_SHA or runtime.get("version") != ACCEPTED_VERSION:
        raise PromotionError(f"O2 runtime mismatch: {runtime}")
    current = SUPERVISOR.deployment_root / "current"
    if not current.is_symlink():
        raise PromotionError("O2 current symlink missing")
    return {"server": _snapshot_service(SUPERVISOR.service_unit), "host": _snapshot_service(SUPERVISOR.host_unit), "runtime": runtime, "current": str(current.resolve()), "health": _health(SUPERVISOR.health_url)}


def _target_guard() -> dict[str, Any]:
    if not _active(TARGET.service_unit) or not _active(TARGET.host_unit):
        raise PromotionError("O1 target services are not both active")
    runtime = identity.runtime_identity(TARGET_VENV / "bin" / "python")
    if runtime.get("commit_sha") != OLD_SHA or runtime.get("version") != OLD_VERSION:
        raise PromotionError(f"O1 is not exact expected 0.8.1: {runtime}")
    integrity, schema, counts = _db(TARGET_DB)
    if integrity != "ok" or schema != OLD_SCHEMA:
        raise PromotionError(f"O1 DB preflight failed: integrity={integrity} schema={schema}")
    return {"server": _snapshot_service(TARGET.service_unit), "host": _snapshot_service(TARGET.host_unit), "runtime": runtime, "schema": schema, "integrity": integrity, "counts": counts, "health": _health(TARGET.health_url)}


def _artifacts() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for name, expected in WHEEL_HASHES.items():
        path = ARTIFACTS / name
        if not path.is_file() or _sha(path) != expected:
            raise PromotionError(f"accepted wheel missing/hash mismatch: {path}")
        if name.startswith("omnigent_client-"):
            out["sdk_client"] = path
        elif name.startswith("omnigent_ui_sdk-"):
            out["sdk_ui"] = path
        else:
            out["main"] = path
    prov = SOURCE_RELEASE / "PROVENANCE.txt"
    text = prov.read_text() if prov.is_file() else ""
    if f"sha={ACCEPTED_SHA}" not in text or f"package_version={ACCEPTED_VERSION}" not in text:
        raise PromotionError("accepted provenance mismatch")
    return out


def _no_live_transactions() -> None:
    """Delegate to the authoritative reconciliation-aware validator."""
    try:
        eligibility.assert_no_blocking_transactions(transaction.DEFAULT_TX_ROOT)
    except transaction.TransactionError as exc:
        raise PromotionError(str(exc)) from exc


def _backup_db(dst: Path) -> str:
    src = sqlite3.connect(str(TARGET_DB)); out = sqlite3.connect(str(dst))
    try:
        with out: src.backup(out)
    finally:
        out.close(); src.close()
    if _db(dst)[0] != "ok":
        raise PromotionError("DB backup integrity failed")
    return _sha(dst)


def _metadata_snapshot(evidence: Path) -> dict[str, bool]:
    state: dict[str, bool] = {}; root = evidence / "metadata-before"; root.mkdir()
    for name in ("PROVENANCE.txt", "DEPLOYED_SHA"):
        src = TARGET_ROOT / name; state[name] = src.is_file()
        if src.is_file(): shutil.copy2(src, root / name)
    _atomic_json(root / "state.json", state)
    return state


def _restore_metadata(evidence: Path, state: dict[str, bool]) -> None:
    root = evidence / "metadata-before"
    for name, existed in state.items():
        dst = TARGET_ROOT / name
        if existed:
            src = root / name
            if not src.is_file(): raise PromotionError(f"metadata backup missing: {src}")
            tmp = TARGET_ROOT / f".{name}.rollback.{os.getpid()}"; shutil.copy2(src, tmp); os.replace(tmp, dst)
        elif dst.exists() or dst.is_symlink(): dst.unlink()


def _stop_target() -> None:
    _svc(TARGET.host_unit, "stop", check=False); _svc(TARGET.service_unit, "stop", check=False); time.sleep(1)
    if _active(TARGET.host_unit) or _active(TARGET.service_unit): raise PromotionError("O1 services did not stop")


def _start_target() -> None:
    _svc(TARGET.service_unit, "start"); _wait_health(TARGET.health_url); _svc(TARGET.host_unit, "start")
    end = time.monotonic() + 30
    while time.monotonic() < end and not _active(TARGET.host_unit): time.sleep(1)
    if not _active(TARGET.host_unit): raise PromotionError("O1 host did not start")


def _prepare_record(tx_id: str, evidence: Path) -> tuple[transaction.TransactionRecord, str]:
    r = transaction.create(tx_id=tx_id, target="O1", supervisor="O2", target_artifact_sha=ACCEPTED_SHA, target_artifact_version=ACCEPTED_VERSION, main_wheel_sha256=WHEEL_HASHES[f"omnigent-{ACCEPTED_VERSION}-py3-none-any.whl"], sdk_client_wheel_sha256=WHEEL_HASHES[f"omnigent_client-{ACCEPTED_VERSION}-py3-none-any.whl"], sdk_ui_wheel_sha256=WHEEL_HASHES[f"omnigent_ui_sdk-{ACCEPTED_VERSION}-py3-none-any.whl"])
    runtime = identity.runtime_identity(TARGET_VENV / "bin" / "python"); mode = "symlink" if TARGET_VENV.is_symlink() else "directory"
    r.old_runtime_sha = runtime["commit_sha"]; r.old_runtime_version = runtime["version"]
    r.old_runtime_path = str(TARGET_VENV.resolve()) if mode == "symlink" else str(TARGET_ROOT / f"venv.legacy-{tx_id}")
    r.new_runtime_path = str(TARGET_ROOT / "releases" / ACCEPTED_SHA); r.new_runtime_sha = ACCEPTED_SHA; r.new_runtime_version = ACCEPTED_VERSION
    r.old_db_schema = OLD_SCHEMA; r.target_db_schema = NEW_SCHEMA; r.log_path = str(evidence / "promotion.log"); transaction.save(r)
    return r, mode


def _stage(r: transaction.TransactionRecord, wheels: dict[str, Path]) -> Path:
    root = staging.transaction_owned_staging_path(TARGET, r.tx_id); final = Path(r.new_runtime_path)
    if final.exists() or final.is_symlink(): raise PromotionError(f"final release already exists; cleanup must prove ownership first: {final}")
    if root.exists(): raise PromotionError(f"TX staging unexpectedly exists: {root}")
    transaction.advance(r, "candidate_staging"); transaction.register_owned(r, str(root))
    staging.stage_candidate_runtime(root, SUPERVISOR, wheels, supervisor_release_root=SOURCE_RELEASE)
    failures = staging.verify_candidate_complete(root)
    if failures or not staging.candidate_identity_matches(root, ACCEPTED_SHA, ACCEPTED_VERSION): raise PromotionError("candidate verification failed: " + "; ".join(failures))
    closure = staging.capture_supervisor_closure(SUPERVISOR); mismatches = staging.verify_candidate_versions(root / "venv" / "bin" / "python", closure.expected_versions())
    if mismatches: raise PromotionError("candidate closure mismatch: " + "; ".join(mismatches))
    transaction.advance(r, "candidate_verified"); return root


def _switch(r: transaction.TransactionRecord, staged: Path, mode: str) -> Path:
    old = Path(r.old_runtime_path); final = Path(r.new_runtime_path)
    if final.exists() or final.is_symlink(): raise PromotionError("final release appeared after preflight")
    if mode == "directory":
        if old.exists(): raise PromotionError(f"TX-specific legacy path exists: {old}")
        os.rename(TARGET_VENV, old)
    else:
        if TARGET_VENV.resolve() != old: raise PromotionError("O1 venv symlink changed after preflight")
        TARGET_VENV.unlink()
    os.rename(staged, final); transaction.register_owned(r, str(final))
    tmp = TARGET_ROOT / f".venv.tmp.{r.tx_id}"; os.symlink(final / "venv", tmp); os.replace(tmp, TARGET_VENV)
    shutil.copy2(final / "PROVENANCE.txt", TARGET_ROOT / "PROVENANCE.txt"); (TARGET_ROOT / "DEPLOYED_SHA").write_text(ACCEPTED_SHA + "\n")
    return final


def _migrate(final: Path) -> None:
    env = {"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONPATH": "", "PYTHONNOUSERSITE": "1", "HOME": "/tmp"}
    _run([str(final / "venv" / "bin" / "python"), "-m", "omnigent.db.migrate", "--database-url", f"sqlite:///{TARGET_DB}"], env=env)
    integrity, schema, _ = _db(TARGET_DB)
    if integrity != "ok" or schema != NEW_SCHEMA: raise PromotionError(f"migration acceptance failed: {integrity}/{schema}")


def _accept(before_counts: dict[str, int]) -> dict[str, Any]:
    runtime = identity.runtime_identity(TARGET_VENV / "bin" / "python"); integrity, schema, counts = _db(TARGET_DB)
    if runtime.get("commit_sha") != ACCEPTED_SHA or runtime.get("version") != ACCEPTED_VERSION: raise PromotionError(f"O1 runtime mismatch after promotion: {runtime}")
    if integrity != "ok" or schema != NEW_SCHEMA: raise PromotionError(f"O1 DB mismatch after promotion: {integrity}/{schema}")
    for name, old in before_counts.items():
        if counts.get(name, -1) < old: raise PromotionError(f"O1 data count regressed: {name} {old}->{counts.get(name)}")
    info = json.loads(_run(["curl", "-fsS", "--max-time", "10", "http://127.0.0.1:4097/v1/info"]).stdout)
    if info.get("server_version") != ACCEPTED_VERSION: raise PromotionError(f"O1 API version mismatch: {info}")
    return {"runtime": runtime, "integrity": integrity, "schema": schema, "counts": counts, "server": _snapshot_service(TARGET.service_unit), "host": _snapshot_service(TARGET.host_unit), "health": _health(TARGET.health_url)}


def _restore_runtime(r: transaction.TransactionRecord, mode: str) -> None:
    old = Path(r.old_runtime_path); candidate = Path(r.new_runtime_path) / "venv"
    if TARGET_VENV.is_symlink():
        current = TARGET_VENV.resolve()
        if current not in (candidate, old): raise PromotionError(f"rollback refuses unknown runtime: {current}")
        TARGET_VENV.unlink()
    elif TARGET_VENV.exists():
        if mode != "directory" or identity.runtime_identity(TARGET_VENV / "bin" / "python").get("commit_sha") != r.old_runtime_sha: raise PromotionError("rollback refuses unknown non-symlink runtime")
        return
    if mode == "directory":
        if not old.is_dir(): raise PromotionError(f"TX old runtime missing: {old}")
        os.rename(old, TARGET_VENV)
    else:
        if not old.exists(): raise PromotionError(f"old symlink target missing: {old}")
        tmp = TARGET_ROOT / f".venv.tmp.rollback.{r.tx_id}"; os.symlink(old, tmp); os.replace(tmp, TARGET_VENV)


def _restore_db(backup: Path, digest: str) -> None:
    if not backup.is_file() or _sha(backup) != digest or _db(backup)[0] != "ok": raise PromotionError("rollback DB backup failed verification")
    for suffix in ("-wal", "-shm"):
        p = Path(str(TARGET_DB) + suffix)
        if p.exists(): p.unlink()
    tmp = TARGET_DB.with_name(TARGET_DB.name + f".rollback.{os.getpid()}"); shutil.copy2(backup, tmp); os.replace(tmp, TARGET_DB); os.chmod(TARGET_DB, 0o644)
    try: shutil.chown(TARGET_DB, user="hermes", group="hermes")
    except LookupError: pass


def _rollback(r: transaction.TransactionRecord, reason: str, backup: Path, digest: str, mode: str, meta: dict[str, bool], evidence: Path) -> dict[str, Any]:
    r.phase = "failure"; r.rollback_reason = reason; r.rollback_completed = False; transaction.save(r)
    _stop_target(); _restore_runtime(r, mode); _restore_db(backup, digest); _restore_metadata(evidence, meta); _start_target()
    runtime = identity.runtime_identity(TARGET_VENV / "bin" / "python"); integrity, schema, _ = _db(TARGET_DB)
    if runtime.get("commit_sha") != r.old_runtime_sha or runtime.get("version") != r.old_runtime_version or integrity != "ok" or schema != r.old_db_schema: raise PromotionError(f"rollback verification failed: runtime={runtime} db={integrity}/{schema}")
    r.phase = "rolled_back"; r.rollback_completed = True; transaction.save(r)
    return {"runtime": runtime, "integrity": integrity, "schema": schema, "health": _health(TARGET.health_url)}


def _signal(signum: int, _frame: Any) -> None:
    raise PromotionInterrupted(f"received signal {signum}")


def install_signal_handlers() -> None:
    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP): signal.signal(sig, _signal)


def run(evidence_root: Path, *, promote: bool) -> int:
    if os.geteuid() != 0: raise PromotionError("must run as root outside Omnigent sandboxes")
    identity.require_distinct(TARGET, SUPERVISOR); _no_live_transactions(); evidence_root.mkdir(parents=True, exist_ok=True)
    supervisor_before = _supervisor_guard(); target_before = _target_guard(); wheels = _artifacts()
    _atomic_json(evidence_root / "01-supervisor-before.json", supervisor_before); _atomic_json(evidence_root / "02-target-before.json", target_before)
    tx_id = transaction.make_tx_id(); evidence = evidence_root / f"tx-{tx_id}"; evidence.mkdir(); r, mode = _prepare_record(tx_id, evidence); transaction.advance(r, "preflight")
    meta = _metadata_snapshot(evidence); transaction.advance(r, "db_backup"); backup = evidence / "chat.db"; backup_digest = _backup_db(backup); r.db_backup_path = str(backup); r.db_backup_sha256 = backup_digest; r.db_backup_integrity = "ok"; transaction.save(r)
    crossed = False; staged: Path | None = None
    try:
        staged = _stage(r, wheels); _atomic_json(evidence / "candidate-verified.json", {"stage": str(staged), "sha": ACCEPTED_SHA, "version": ACCEPTED_VERSION})
        if not promote:
            staging.safe_cleanup_staging(TARGET, tx_id); r.phase = "failure"; r.rollback_reason = "preflight-only; no mutation"; transaction.save(r)
            print(json.dumps({"verdict": "READY — V3 PREFLIGHT PASSED; NO MUTATION", "tx_id": tx_id}, indent=2)); return 0
        transaction.cross_mutation_boundary(r); crossed = True; (evidence / "MUTATION_BOUNDARY_CROSSED").write_text("true\n")
        transaction.advance(r, "switch"); _stop_target(); final = _switch(r, staged, mode); _migrate(final); transaction.advance(r, "service_restart"); _start_target(); transaction.advance(r, "acceptance"); target_after = _accept(target_before["counts"]); _atomic_json(evidence / "target-after.json", target_after)
        supervisor_after = _supervisor_guard(); _atomic_json(evidence_root / "03-supervisor-after.json", supervisor_after)
        if supervisor_after != supervisor_before: raise PromotionError("O2 supervisor guard changed")
        transaction.complete(r); result = {"verdict": "COMPLETE — O2 SUPERVISED O1 PROMOTION TO ACCEPTED 0.9 ARTIFACT", "tx_id": tx_id, "sha": ACCEPTED_SHA, "version": ACCEPTED_VERSION, "schema": NEW_SCHEMA}; _atomic_json(evidence / "SUMMARY.json", result); print(json.dumps(result, indent=2)); return 0
    except BaseException as exc:
        reason = f"{type(exc).__name__}: {exc}"
        if not crossed:
            r.phase = "failure"; r.rollback_reason = reason; transaction.save(r)
            if staged is not None or (TARGET_ROOT / "staging" / tx_id).exists(): staging.safe_cleanup_staging(TARGET, tx_id)
            result = {"verdict": "STOP — PRE-MUTATION FAILURE; O1 UNTOUCHED", "tx_id": tx_id, "reason": reason}; _atomic_json(evidence / "SUMMARY.json", result); print(json.dumps(result, indent=2)); return 2
        try:
            rb = _rollback(r, reason, backup, backup_digest, mode, meta, evidence); supervisor_after = _supervisor_guard(); result = {"verdict": "ROLLED BACK — O1 PEER PROMOTION FAILED SAFELY", "tx_id": tx_id, "reason": reason, "rollback": rb, "supervisor_zero_drift": supervisor_after == supervisor_before}; _atomic_json(evidence / "SUMMARY.json", result); print(json.dumps(result, indent=2)); return 3
        except BaseException as rb_exc:
            r.phase = "failure"; r.rollback_completed = False; r.rollback_reason = reason + f"; rollback failed: {rb_exc}"; transaction.save(r); result = {"verdict": "CRITICAL — O1 PROMOTION AND AUTOMATIC ROLLBACK FAILED", "tx_id": tx_id, "reason": reason, "rollback_failure": f"{type(rb_exc).__name__}: {rb_exc}"}; _atomic_json(evidence / "SUMMARY.json", result); print(json.dumps(result, indent=2)); return 4


__all__ = ["PromotionError", "install_signal_handlers", "run"]
