"""Permanent root-owned Control Room peer-deployer service.

The service exposes a tiny JSON protocol over a local Unix-domain socket.
It authenticates callers using Linux peer credentials and the caller's
systemd cgroup, not caller-supplied identity.  Because O1 and O2 currently
share the same Unix UID (hermes), UID is only a coarse precondition; the
specific systemd unit cgroup distinguishes O1 from O2.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import re
import socket
import socketserver
import subprocess
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from . import eligibility, host_promotion, identity, preflight, transaction

CONTROL_ROOT = Path(os.environ.get("CRPD_ROOT", "/var/lib/control-room-peer-deployer"))
RUN_ROOT = Path(os.environ.get("CRPD_RUN", "/run/control-room-peer-deployer"))
SOCKET_PATH = RUN_ROOT / "control.sock"


def _control_root() -> Path:
    return CONTROL_ROOT


def _run_root() -> Path:
    return RUN_ROOT
LOCK_PATH = CONTROL_ROOT / "locks" / "deployment.lock"
EVIDENCE_ROOT = CONTROL_ROOT / "evidence"
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_OPS = {"status", "preflight", "promote"}
ALLOWED_KEYS = {
    "status": {"op", "tx_id"},
    "preflight": {"op", "target", "accepted_sha", "request_id"},
    "promote": {"op", "target", "accepted_sha", "request_id"},
}
CGROUP_UNITS = {
    "O1": {"omnigent.service", "omnigent-host.service"},
    "O2": {"omnigent-production.service", "omnigent-production-host.service"},
}
PROMOTION_ALLOWED_SHA = preflight.ACCEPTED_ARTIFACT_SHA
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,96}$")
TX_ID_RE = re.compile(r"^promotion-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")


class ProtocolError(RuntimeError):
    pass


class AuthorizationError(RuntimeError):
    pass


@dataclass
class TransactionStatus:
    tx_id: str
    target: str
    supervisor: str
    state: str
    returncode: int | None = None
    started_at: float | None = None
    finished_at: float | None = None
    summary: dict[str, Any] | None = None
    log_tail: list[str] | None = None


class DeploymentManager:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._mu = threading.Lock()
        self._active: dict[str, threading.Thread] = {}
        for p in (_control_root() / "transactions", _control_root() / "evidence", _control_root() / "locks", _control_root() / "artifacts"):
            p.mkdir(parents=True, exist_ok=True)

    def submit(self, *, target_name: str, supervisor_name: str, promote: bool) -> TransactionStatus:
        with self._mu:
            if self._active:
                raise ProtocolError("REFUSED: deployment already active")
            eligibility.assert_no_blocking_transactions()
            tx_id = transaction.make_tx_id()
            status = TransactionStatus(tx_id, target_name, supervisor_name, "queued", started_at=time.time())
            t = threading.Thread(target=self._run, args=(status, promote), daemon=True)
            self._active[tx_id] = t
            t.start()
            return status

    def _run(self, status: TransactionStatus, promote: bool) -> None:
        status.state = "running"
        evidence = _control_root() / "evidence" / status.tx_id
        log_path = evidence / "service.log"
        evidence.mkdir(parents=True, exist_ok=True)
        rc = 99
        try:
            with open(_control_root() / "locks" / "deployment.lock", "w") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self.dry_run:
                    (evidence / "DRY_RUN").write_text("true\n")
                    rc = 0
                    status.summary = {"verdict": "DRY_RUN", "tx_id": status.tx_id}
                else:
                    with log_path.open("a") as log:
                        old_stdout = os.dup(1); old_stderr = os.dup(2)
                        try:
                            os.dup2(log.fileno(), 1); os.dup2(log.fileno(), 2)
                            # host_promotion is intentionally the hardened state machine.
                            rc = host_promotion.run(evidence, promote=promote)
                        finally:
                            os.dup2(old_stdout, 1); os.dup2(old_stderr, 2)
                            os.close(old_stdout); os.close(old_stderr)
                    summary = next(evidence.glob("tx-*/SUMMARY.json"), None)
                    if summary and summary.is_file():
                        status.summary = json.loads(summary.read_text())
        except BlockingIOError:
            status.state = "refused"
            status.summary = {"error": "deployment lock held"}
            rc = 75
        except BaseException as exc:
            status.state = "failed"
            status.summary = {"error": f"{type(exc).__name__}: {exc}"}
            rc = 70
        finally:
            status.returncode = rc
            status.finished_at = time.time()
            if status.state not in {"failed", "refused"}:
                status.state = "succeeded" if rc == 0 else "failed"
            if log_path.is_file():
                status.log_tail = log_path.read_text(errors="replace").splitlines()[-80:]
            with self._mu:
                self._active.pop(status.tx_id, None)

    def status(self, tx_id: str | None = None) -> dict[str, Any]:
        with self._mu:
            active = [asdict(TransactionStatus(k, "", "", "running")) for k in self._active]
        decisions = [asdict(d) for d in eligibility.deployment_eligibility()]
        return {"ok": True, "active": active, "transaction_eligibility": decisions, "service": "control-room-peer-deployer"}


def _pid_cgroup_unit(pid: int) -> str:
    text = Path(f"/proc/{pid}/cgroup").read_text(errors="replace")
    for line in text.splitlines():
        if ".service" in line:
            part = line.rsplit("/", 1)[-1]
            if part.endswith(".service"):
                return part
    raise AuthorizationError("caller is not in a recognized systemd service cgroup")


def authenticated_instance(pid: int, uid: int) -> str:
    hermes_uid = pwd.getpwnam("hermes").pw_uid
    if uid != hermes_uid:
        raise AuthorizationError("caller uid is not the Omnigent service uid")
    unit = _pid_cgroup_unit(pid)
    for inst, units in CGROUP_UNITS.items():
        if unit in units:
            return inst
    raise AuthorizationError(f"untrusted caller unit: {unit}")


def validate_request(blob: Any) -> dict[str, Any]:
    if not isinstance(blob, dict):
        raise ProtocolError("request must be a JSON object")
    op = blob.get("op")
    if op not in ALLOWED_OPS:
        raise ProtocolError("unknown operation")
    extra = set(blob) - ALLOWED_KEYS[op]
    if extra:
        raise ProtocolError(f"unexpected request keys: {sorted(extra)}")
    if op in {"preflight", "promote"}:
        if blob.get("target") not in identity.REGISTRY:
            raise ProtocolError("invalid target")
        if blob.get("accepted_sha") != PROMOTION_ALLOWED_SHA:
            raise ProtocolError("accepted_sha is not allow-listed")
        rid = blob.get("request_id")
        if rid is not None and (not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid)):
            raise ProtocolError("invalid request_id")
    if op == "status" and blob.get("tx_id") is not None:
        if not isinstance(blob["tx_id"], str) or not TX_ID_RE.fullmatch(blob["tx_id"]):
            raise ProtocolError("invalid tx_id")
    return blob


class Handler(socketserver.StreamRequestHandler):
    manager: DeploymentManager

    def handle(self) -> None:
        try:
            data = self.rfile.readline(MAX_REQUEST_BYTES + 1)
            if len(data) > MAX_REQUEST_BYTES:
                raise ProtocolError("request too large")
            req = validate_request(json.loads(data.decode()))
            creds = self.request.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12)
            pid = int.from_bytes(creds[0:4], "little")
            uid = int.from_bytes(creds[4:8], "little")
            caller = authenticated_instance(pid, uid)
            op = req["op"]
            if op == "status":
                resp = self.manager.status(req.get("tx_id"))
            else:
                target = req["target"]
                if caller == target:
                    raise AuthorizationError(f"REFUSED: authenticated {caller} may not upgrade itself")
                if (caller, target) not in {("O1", "O2"), ("O2", "O1")}:
                    raise AuthorizationError("REFUSED: topology violation")
                supervisor = identity.get(caller); target_i = identity.get(target)
                identity.require_distinct(target_i, supervisor)
                if op == "preflight":
                    report = preflight.run_preflight(target=target_i, supervisor=supervisor)
                    resp = {"ok": report.passed, "caller": caller, "target": target, "preflight": report.to_dict()}
                else:
                    st = self.manager.submit(target_name=target, supervisor_name=caller, promote=True)
                    resp = {"ok": True, "caller": caller, "target": target, "tx_id": st.tx_id, "state": st.state}
        except Exception as exc:
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(resp, sort_keys=True) + "\n").encode())


class UnixServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True


def serve(sock: Path = SOCKET_PATH, *, dry_run: bool = False) -> None:
    _run_root().mkdir(parents=True, exist_ok=True)
    try:
        sock.unlink()
    except FileNotFoundError:
        pass
    manager = DeploymentManager(dry_run=dry_run)
    Handler.manager = manager
    with UnixServer(str(sock), Handler) as srv:
        os.chmod(sock, 0o666)
        srv.serve_forever()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--socket", type=Path, default=SOCKET_PATH)
    ap.add_argument("--dry-run", action="store_true")
    ns = ap.parse_args(argv)
    serve(ns.socket, dry_run=ns.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
