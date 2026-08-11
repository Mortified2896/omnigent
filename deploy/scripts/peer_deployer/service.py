"""Permanent root-owned Control Room peer-deployer service.

The service exposes a tiny JSON protocol over a local Unix-domain
socket.  It authenticates callers using Linux peer credentials and
the caller's systemd cgroup, not caller-supplied identity.  Because
O1 and O2 share the same Unix UID, the cgroup is what distinguishes
them.

Topology, plans, and accepted artifacts are loaded ONCE per daemon
start from root-owned files:

  * /var/lib/control-room-peer-deployer/artifacts/registry.json
  * /var/lib/control-room-peer-deployer/plans/*.json

The daemon does NOT have a built-in ``O2 -> O1`` hardcoded default.
The bidirectional design is real: whichever (caller, target) pair
the operator bootstrapped a plan for, the daemon will execute.  If
no plan exists, the daemon REFUSES.

The service protocol is intentionally narrow.  It only accepts the
keys needed for one of three operations: ``status``, ``preflight``,
``promote``.  Anything else is a hard refusal.
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

from . import eligibility, engine, identity, transaction
from .plan import PlanError, PromotionPlan, find as find_plan
from .registry import RegistryError, TrustedRegistry, load as load_registry

CONTROL_ROOT = Path(os.environ.get("CRPD_ROOT", "/var/lib/control-room-peer-deployer"))
RUN_ROOT = Path(os.environ.get("CRPD_RUN", "/run/control-room-peer-deployer"))
SOCKET_PATH = RUN_ROOT / "control.sock"
LOCK_PATH = CONTROL_ROOT / "locks" / "deployment.lock"
EVIDENCE_ROOT = CONTROL_ROOT / "evidence"
TX_ROOT = CONTROL_ROOT / "transactions"
REGISTRY_PATH = CONTROL_ROOT / "artifacts" / "registry.json"
PLANS_DIR = CONTROL_ROOT / "plans"
MAX_REQUEST_BYTES = 64 * 1024
ALLOWED_OPS = {"status", "preflight", "promote", "installer_health"}
ALLOWED_KEYS = {
    "status": {"op", "tx_id"},
    "preflight": {"op", "target", "request_id"},
    "promote": {"op", "target", "request_id"},
    "installer_health": {"op"},
}
CGROUP_UNITS = {
    "O1": {"omnigent.service", "omnigent-host.service"},
    "O2": {"omnigent-production.service", "omnigent-production-host.service"},
}
REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_.:@+-]{1,96}$")
TX_ID_RE = re.compile(r"^promotion-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

# Default transaction root for the daemon; redirects the legacy
# /var/lib/omnigent-control-room/transactions path to the daemon's
# own root-owned transactions dir.
TRANSACTION_ROOT = TX_ROOT
engine.RUNTIME_TRANSACTION_ROOT = lambda: TRANSACTION_ROOT  # type: ignore[assignment]


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


class TrustedConfig:
    """Holder for the root-owned registry + plans, loaded at daemon start."""

    def __init__(self) -> None:
        self.registry: TrustedRegistry = load_registry(REGISTRY_PATH)
        self.plans: dict[tuple[str, str], PromotionPlan] = {}

    def load_plans(self) -> None:
        self.plans.clear()
        self.plans.update(_load_all_plans(PLANS_DIR))

    def plan_for(self, caller: str, target: str) -> PromotionPlan:
        if not self.plans:
            self.load_plans()
        return find_plan(caller, target, PLANS_DIR)

    def has_plan_for(self, caller: str, target: str) -> bool:
        if not self.plans:
            self.load_plans()
        return (caller, target) in self.plans


def _load_all_plans(plan_dir: Path) -> dict[tuple[str, str], PromotionPlan]:
    from .plan import load_all

    return load_all(plan_dir)


class DeploymentManager:
    def __init__(self, *, dry_run: bool = False) -> None:
        self.dry_run = dry_run
        self._mu = threading.Lock()
        self._active: dict[str, threading.Thread] = {}
        # The daemon is the only writer of state under its control
        # root.  We create directories here so unit tests can mount a
        # tmpfs-style sandbox without crashing.  In production these
        # directories are created by the bootstrap installer with the
        # correct ownership; mkdir with exist_ok is fine.
        for p in (TRANSACTION_ROOT, EVIDENCE_ROOT, LOCK_PATH.parent,
                  REGISTRY_PATH.parent, PLANS_DIR):
            try:
                p.mkdir(parents=True, exist_ok=True)
            except OSError:
                # Read-only file system fallback for unit tests; the
                # bootstrap installer already populated these paths.
                pass
        self.trusted = TrustedConfig()

    def submit(
        self,
        *,
        plan: PromotionPlan,
        promote: bool,
    ) -> TransactionStatus:
        with self._mu:
            if self._active:
                raise ProtocolError("REFUSED: deployment already active")
            eligibility.assert_no_blocking_transactions(root=TX_ROOT)
            tx_id = transaction.make_tx_id()
            status = TransactionStatus(
                tx_id=tx_id,
                target=plan.target_name,
                supervisor=plan.supervisor_name,
                state="queued",
                started_at=time.time(),
            )
            t = threading.Thread(
                target=self._run,
                args=(status, plan, promote),
                daemon=True,
            )
            self._active[tx_id] = t
            t.start()
            return status

    def _run(
        self,
        status: TransactionStatus,
        plan: PromotionPlan,
        promote: bool,
    ) -> None:
        status.state = "running"
        evidence = CONTROL_ROOT / "evidence" / status.tx_id
        log_path = evidence / "service.log"
        evidence.mkdir(parents=True, exist_ok=True)
        rc = 99
        try:
            with open(LOCK_PATH, "w") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                if self.dry_run:
                    (evidence / "DRY_RUN").write_text("true\n")
                    rc = 0
                    status.summary = {
                        "verdict": "DRY_RUN",
                        "tx_id": status.tx_id,
                    }
                else:
                    with log_path.open("a") as log:
                        old_stdout = os.dup(1)
                        old_stderr = os.dup(2)
                        try:
                            os.dup2(log.fileno(), 1)
                            os.dup2(log.fileno(), 2)
                            outcome = engine.run_promotion(
                                plan=plan,
                                registry=self.trusted.registry,
                                evidence_root=evidence,
                                promote=promote,
                            )
                            rc = 0 if outcome.verdict == "PROMOTION COMMITTED" else (
                                2 if "PRE-MUTATION" in outcome.verdict else 3
                            )
                            status.summary = {
                                "verdict": outcome.verdict,
                                "tx_id": status.tx_id,
                                "sha": outcome.sha,
                                "version": outcome.version,
                                "reason": outcome.reason,
                            }
                        finally:
                            os.dup2(old_stdout, 1)
                            os.dup2(old_stderr, 2)
                            os.close(old_stdout)
                            os.close(old_stderr)
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
            active = [
                asdict(TransactionStatus(k, "", "", "running"))
                for k in self._active
            ]
        decisions = [
            asdict(d)
            for d in eligibility.deployment_eligibility(root=TX_ROOT)
        ]
        return {
            "ok": True,
            "active": active,
            "transaction_eligibility": decisions,
            "service": "control-room-peer-deployer",
        }


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
        target = blob.get("target")
        if target not in {"O1", "O2"}:
            raise ProtocolError("invalid target")
        rid = blob.get("request_id")
        if rid is not None and (
            not isinstance(rid, str) or not REQUEST_ID_RE.fullmatch(rid)
        ):
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
            creds = self.request.getsockopt(
                socket.SOL_SOCKET, socket.SO_PEERCRED, 12
            )
            pid = int.from_bytes(creds[0:4], "little")
            uid = int.from_bytes(creds[4:8], "little")
            caller = authenticated_instance(pid, uid)
            op = req["op"]
            if op == "installer_health":
                # Installer-only local verification. This op is
                # callable ONLY when the caller's UID is root (e.g. the
                # bootstrap installer). It returns a minimal health
                # snapshot and explicitly CANNOT perform promotion or
                # preflight. It exists to give the post-install check
                # a non-mutating, root-only proof that the daemon is
                # alive and the registry is loadable.
                if uid != 0:
                    raise AuthorizationError(
                        "installer_health is root-only and cannot be "
                        "called from the application UIDs"
                    )
                resp = {
                    "ok": True,
                    "service": "control-room-peer-deployer",
                    "scope": "installer_health",
                    "registry_loadable": True,
                    "process_pid": os.getpid(),
                }
            elif op == "status":
                resp = self.manager.status(req.get("tx_id"))
            else:
                target = req["target"]
                if caller == target:
                    raise AuthorizationError(
                        f"REFUSED: authenticated {caller} may not upgrade itself"
                    )
                if caller not in {"O1", "O2"} or target not in {"O1", "O2"}:
                    raise AuthorizationError(
                        "REFUSED: topology violation (caller/target)"
                    )
                if (caller, target) not in {("O1", "O2"), ("O2", "O1")}:
                    raise AuthorizationError(
                        "REFUSED: topology violation (must be O1<->O2)"
                    )
                try:
                    plan = self.manager.trusted.plan_for(caller, target)
                except (PlanError,) as exc:
                    raise AuthorizationError(
                        f"REFUSED: no accepted plan for {caller} -> {target}: {exc}"
                    ) from exc
                # Re-prove the supervisor==caller, target==target invariants
                if plan.supervisor_name != caller or plan.target_name != target:
                    raise AuthorizationError(
                        "REFUSED: plan supervisor/target do not match caller/target"
                    )
                if op == "preflight":
                    # Preflight-only: load artifact identity, verify health probes,
                    # but never mutate.
                    try:
                        artifact_sha = plan.accepted_artifact_sha
                        artifact = self.manager.trusted.registry.get(artifact_sha)
                    except RegistryError as exc:
                        raise AuthorizationError(
                            f"REFUSED: artifact not in trusted registry: {exc}"
                        ) from exc
                    resp = {
                        "ok": True,
                        "caller": caller,
                        "target": target,
                        "accepted_sha": artifact.artifact_sha,
                        "accepted_version": artifact.version,
                        "supervisor_health": _quick_health(plan.supervisor_health_url),
                        "target_health": _quick_health(plan.target_health_url),
                    }
                else:
                    st = self.manager.submit(plan=plan, promote=True)
                    resp = {
                        "ok": True,
                        "caller": caller,
                        "target": target,
                        "tx_id": st.tx_id,
                        "state": st.state,
                    }
        except Exception as exc:
            resp = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        self.wfile.write((json.dumps(resp, sort_keys=True) + "\n").encode())


def _quick_health(url: str) -> str:
    """Best-effort localhost http probe; never raises."""
    try:
        result = subprocess.run(
            ["curl", "-fsS", "--max-time", "3", url],
            capture_output=True,
            text=True,
            check=False,
            env={
                "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
            },
        )
    except FileNotFoundError:
        return "no-curl"
    return result.stdout.strip() if result.returncode == 0 else f"unhealthy:{result.returncode}"


class UnixServer(socketserver.ThreadingUnixStreamServer):
    allow_reuse_address = True


def serve(sock: Path = SOCKET_PATH, *, dry_run: bool = False) -> None:
    RUN_ROOT.mkdir(parents=True, exist_ok=True)
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
