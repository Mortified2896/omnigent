"""Externally controlled RTX O1/O2 release promotion and recovery.

Unlike :mod:`peer_deployer.rtx`, this entrypoint never accepts a supervisor.
The independent caller remains alive while it promotes one target at a time.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import socket
import stat
import subprocess
from pathlib import Path
from urllib.request import urlopen

from . import rtx
from .rtx_contract import Peer, Refused, canonical_digest, digest, durable_json, require

_TX_ID = re.compile(r"external-[a-z0-9][a-z0-9-]{7,70}")
_SHA = re.compile(r"[a-f0-9]{40}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
_RELEASE_ROOT = Path("/srv/omnigent/releases")
_MIN_FREE_BYTES = 1024**3


class ExternalJournal:
    """Durable transaction journal with no O1/O2 supervisor identity."""

    def __init__(self, path: Path, record: dict):
        self.path = path
        self.record = record

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        target: Peer,
        expected: str,
        old: str,
        accepted: str,
        artifact_digest: str,
        transaction_id: str,
    ) -> ExternalJournal:
        require(_TX_ID.fullmatch(transaction_id) is not None, "invalid external transaction ID")
        require(_SHA.fullmatch(expected) is not None, "expected-current SHA required")
        require(not path.exists(), "transaction replay refused")
        record = {
            "controller": "external",
            "transaction_id": transaction_id,
            "target": target.document(),
            "expected_current_sha": expected,
            "old_release": old,
            "accepted_release": accepted,
            "acceptance_digest": artifact_digest,
            "mutation_boundary": False,
            "database_mutated": False,
            "status": "preflight",
            "owned": [str(target.current), str(target.db), target.unit, target.host_unit],
            "preserve": [old, accepted],
            "backup": None,
        }
        durable_json(path, record, exclusive=True)
        return cls(path, record)

    def save(self, **changes: object) -> None:
        require(
            not (self.record["mutation_boundary"] and changes.get("mutation_boundary") is False),
            "mutation boundary cannot be cleared",
        )
        self.record.update(changes)
        durable_json(self.path, self.record)

    def authorize_rollback(self, resources: list[str]) -> None:
        require(self.record["mutation_boundary"], "rollback before mutation refused")
        require(
            self.record["status"] not in {"committed", "rolled_back"},
            "terminal replay refused",
        )
        require(
            set(resources) <= set(self.record["owned"]),
            "rollback of unowned resource refused",
        )


def external_controller_guard() -> None:
    """Prove this caller is outside both service identities and cgroups."""
    require(socket.gethostname() == "rtx-omnigent", "RTX host required")
    require(os.geteuid() == 0, "root controller required")
    require(not os.environ.get("OMNIGENT_INSTANCE_ID"), "instance-controlled caller refused")
    pid = os.getpid()
    seen: set[int] = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            environment = (proc / "environ").read_bytes().split(b"\0")
            cgroup = (proc / "cgroup").read_text()
            require(
                not any(item.startswith(b"OMNIGENT_INSTANCE_ID=") for item in environment),
                "instance-controlled ancestor refused",
            )
            require(
                "omnigent-o1" not in cgroup and "omnigent-o2" not in cgroup,
                "instance cgroup caller refused",
            )
            status = dict(
                line.split(":", 1) for line in (proc / "status").read_text().splitlines()
            )
            pid = int(status["PPid"].strip())
        except (FileNotFoundError, ProcessLookupError):
            break


def _health(peer: Peer) -> dict:
    with urlopen(f"http://127.0.0.1:{peer.port}/health", timeout=5) as response:
        require(response.status == 200, "target health status is not 200")
        body = json.load(response)
    require(body.get("status") == "ok", "target health response is not ok")
    return body


def _state_bytes(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        require(not item.is_symlink(), f"state contains an indirect path: {item}")
        if item.is_file():
            total += item.stat().st_size
    return total


def _check_headroom(peer: Peer) -> None:
    # The stopped-target SQLite backup and retained state archive must fit with
    # room for SQLite journaling and a bounded failed-start recovery.
    backup_bytes = _state_bytes(peer.root / "state") + peer.db.stat().st_size
    required = backup_bytes * 2 + _MIN_FREE_BYTES
    free = shutil.disk_usage("/srv/omnigent").free
    require(free >= required, "insufficient disk headroom for backup and rollback")


def _auth_overlay_dropin_path(target: Peer) -> Path:
    return Path(
        f"/etc/systemd/system/omnigent-{target.instance.lower()}.service.d/"
        "50-tailscale-auth-overlay.conf"
    )


def _run_as_service(args: list[str]) -> None:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
        cwd="/tmp",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin"},
    )
    require(result.returncode == 0, "candidate acceptance is not accessible to hermes")


def _ensure_acceptance_readable_by_service(path: Path) -> None:
    """Expose only the validated candidate record to the hermes service user.

    ``rtx.accepted`` has already verified the immutable, root-owned record and
    its release.  The server reads that record during startup, so a private
    0700 artifact directory would make an otherwise valid candidate fail its
    bounded health check after restart.  Restrict this repair to the one
    acceptance directory and file; do not chmod the artifact root or release.
    """
    require(
        path.name == "acceptance-v2.json"
        and path.parent.parent == rtx.ARTIFACTS
        and _SHA.fullmatch(path.parent.name) is not None,
        "acceptance path is outside the canonical candidate artifact",
    )
    parent = path.parent
    require(
        not parent.is_symlink() and stat.S_ISDIR(parent.lstat().st_mode),
        "candidate acceptance directory must be a real directory",
    )
    require(
        not path.is_symlink() and stat.S_ISREG(path.lstat().st_mode),
        "candidate acceptance record must be a regular file",
    )

    parent.chmod(0o755)
    path.chmod(0o444)
    require(
        stat.S_IMODE(parent.lstat().st_mode) == 0o755,
        "candidate directory mode repair failed",
    )
    require(stat.S_IMODE(path.lstat().st_mode) == 0o444, "candidate record mode repair failed")
    _run_as_service(["runuser", "-u", "hermes", "--", "test", "-x", str(parent)])
    _run_as_service(["runuser", "-u", "hermes", "--", "test", "-r", str(path)])


def promote(
    target: Peer,
    expected_sha: str,
    acceptance_path: Path,
    acceptance_digest: str,
    transaction_id: str,
) -> dict:
    external_controller_guard()
    require(_SHA.fullmatch(expected_sha) is not None, "expected-current SHA required")
    require(_SHA256.fullmatch(acceptance_digest) is not None, "invalid acceptance digest")
    require(_TX_ID.fullmatch(transaction_id) is not None, "invalid external transaction ID")
    with rtx.locked(rtx.TRANSACTIONS):
        candidate = rtx.accepted(acceptance_path, acceptance_digest)
        _ensure_acceptance_readable_by_service(acceptance_path)
        # The permission-only repair must leave the accepted bytes unchanged.
        candidate = rtx.accepted(acceptance_path, acceptance_digest)
        require(candidate["source_sha"] != expected_sha, "candidate is already active")
        before = rtx.snapshot(target, expected_sha)
        _health(target)
        require(before["database"]["schema"] == candidate["schema"], "DB schema mismatch")
        _check_headroom(target)

        directory = rtx.TRANSACTIONS / transaction_id
        directory.mkdir(mode=0o700)
        tx = ExternalJournal.create(
            directory / "transaction.json",
            target=target,
            expected=expected_sha,
            old=str(target.current.resolve()),
            accepted=candidate["runtime"],
            artifact_digest=acceptance_digest,
            transaction_id=transaction_id,
        )
        try:
            require(rtx.snapshot(target, expected_sha) == before, "target drift")
            _health(target)
            tx.save(mutation_boundary=True, status="stopping")
            rtx.stop(target)
            tx.save(backup=rtx.backup(target, directory), status="backed_up")
            rtx.accepted(acceptance_path, acceptance_digest)
            rtx.switch(target, Path(candidate["runtime"]), tx)
            tx.save(database_mutated=True, status="starting")
            after = rtx.start(target, candidate["source_sha"])
            _health(target)
            tx.save(status="committed", target_before=before, target_after=after)
        except BaseException as exc:
            tx.save(error=type(exc).__name__ + ": " + str(exc))
            if tx.record["mutation_boundary"]:
                rtx.rollback(target, tx)  # type: ignore[arg-type]
            else:
                tx.save(status="refused")
            raise
        return tx.record


def recover(target: Peer, transaction_id: str) -> dict:
    external_controller_guard()
    require(_TX_ID.fullmatch(transaction_id) is not None, "invalid external transaction ID")
    with rtx.locked(rtx.TRANSACTIONS, recovering=transaction_id):
        path = rtx.TRANSACTIONS / transaction_id / "transaction.json"
        rtx.trusted(path)
        record = json.loads(path.read_text())
        require(record.get("controller") == "external", "not an external transaction")
        require(record.get("transaction_id") == transaction_id, "transaction identity mismatch")
        require(record.get("target") == target.document(), "transaction target mismatch")
        require(
            record.get("status") not in {"committed", "rolled_back", "refused"},
            "terminal replay refused",
        )
        tx = ExternalJournal(path, record)
        if not record["mutation_boundary"]:
            require(
                target.current.is_symlink()
                and str(target.current.resolve()) == record["old_release"],
                "pre-mutation transaction observed a changed release pointer",
            )
            tx.save(status="refused", recovery="confirmed no active mutation occurred")
            return tx.record
        for key in ("old_release", "accepted_release"):
            release = Path(record[key])
            require(release.parent == Path("/srv/omnigent/releases"), "unowned release path")
            rtx.trusted(release)
        rtx.rollback(target, tx)  # type: ignore[arg-type]
        return tx.record


def rollback_committed(
    target: Peer,
    transaction_id: str,
    rollback_transaction_id: str,
    auth_overlay_sha256: str,
) -> dict:
    """Roll back a committed external release after post-deploy acceptance fails.

    This operation restores only the target release and the task-specific
    Tailscale auth drop-in. It deliberately keeps the live database in place;
    no database backup is restored or replaced. The original committed journal
    remains immutable and the rollback receives its own journal.
    """
    external_controller_guard()
    require(_TX_ID.fullmatch(transaction_id) is not None, "invalid external transaction ID")
    require(
        _TX_ID.fullmatch(rollback_transaction_id) is not None,
        "invalid rollback transaction ID",
    )
    require(transaction_id != rollback_transaction_id, "rollback transaction must be distinct")
    require(
        _SHA256.fullmatch(auth_overlay_sha256) is not None,
        "invalid auth overlay SHA-256",
    )
    with rtx.locked(rtx.TRANSACTIONS):
        source_path = rtx.TRANSACTIONS / transaction_id / "transaction.json"
        rtx.trusted(source_path)
        source = json.loads(source_path.read_text())
        require(source.get("controller") == "external", "not an external transaction")
        require(source.get("transaction_id") == transaction_id, "transaction identity mismatch")
        require(source.get("target") == target.document(), "transaction target mismatch")
        require(source.get("status") == "committed", "source transaction is not committed")
        require(
            source.get("mutation_boundary") is True,
            "source transaction did not mutate target",
        )

        active = Path(source["accepted_release"])
        previous = Path(source["old_release"])
        for release in (active, previous):
            require(release.parent == _RELEASE_ROOT, "unowned release path")
            rtx.trusted(release)
        require(target.current.is_symlink(), "current release pointer missing")
        require(target.current.resolve() == active, "active release drifted after deployment")
        active_sha = active.name
        previous_sha = previous.name
        require(_SHA.fullmatch(active_sha) is not None, "invalid active release identity")
        require(_SHA.fullmatch(previous_sha) is not None, "invalid previous release identity")
        require(
            source.get("expected_current_sha") == previous_sha,
            "previous release identity mismatch",
        )

        active_artifact = rtx.accepted(
            rtx.ARTIFACTS / active_sha / "acceptance-v2.json",
            source["acceptance_digest"],
        )
        previous_artifact_path = rtx.ARTIFACTS / previous_sha / "acceptance-v2.json"
        rtx.trusted(previous_artifact_path)
        previous_artifact_payload = json.loads(previous_artifact_path.read_text())
        previous_artifact_digest = canonical_digest(previous_artifact_payload)
        previous_artifact = rtx.accepted(previous_artifact_path, previous_artifact_digest)
        require(active_artifact["runtime"] == str(active), "active release artifact mismatch")
        require(
            previous_artifact["runtime"] == str(previous),
            "previous release artifact mismatch",
        )
        require(
            active_artifact["schema"] == previous_artifact["schema"],
            "database schema differs between releases",
        )

        dropin = _auth_overlay_dropin_path(target)
        rtx.trusted(dropin)
        require(dropin.is_file() and not dropin.is_symlink(), "auth overlay drop-in is not a file")
        require(digest(dropin) == auth_overlay_sha256, "auth overlay drop-in changed")

        before = rtx.snapshot(target, active_sha)
        _health(target)
        database_before = rtx.database_evidence(target)
        require(
            database_before["schema"] == active_artifact["schema"],
            "live database schema differs from active release",
        )
        directory = rtx.TRANSACTIONS / rollback_transaction_id
        directory.mkdir(mode=0o700)
        tx = ExternalJournal.create(
            directory / "transaction.json",
            target=target,
            expected=active_sha,
            old=str(active),
            accepted=str(previous),
            artifact_digest=previous_artifact_digest,
            transaction_id=rollback_transaction_id,
        )
        tx.save(
            owned=[*tx.record["owned"], str(dropin)],
            operation="post-acceptance-rollback",
            source_transaction_id=transaction_id,
            database_restored=False,
            config_restore={"path": str(dropin), "sha256": auth_overlay_sha256, "state": "absent"},
        )
        try:
            require(rtx.snapshot(target, active_sha) == before, "target drift before rollback")
            _health(target)
            require(digest(dropin) == auth_overlay_sha256, "auth overlay drop-in changed")
            tx.save(mutation_boundary=True, status="stopping")
            rtx.stop(target)
            dropin.unlink()
            rtx.run(["systemctl", "daemon-reload"])
            tx.save(status="config_restored")
            rtx.switch(target, previous, tx)
            tx.save(status="starting")
            startup = rtx.start(target, previous_sha)
            _health(target)
            after = rtx.snapshot(target, previous_sha)
            database_after = rtx.database_evidence(target)
            require(
                database_after["identity"] == database_before["identity"]
                and database_after["schema"] == database_before["schema"],
                "database identity or schema changed during rollback",
            )
            tx.save(
                status="rolled_back",
                target_before=before,
                target_after=after,
                startup=startup,
                database_before=database_before,
                database_after=database_after,
                rollback_evidence={"release": str(previous), "auth_overlay": "absent"},
            )
        except BaseException as exc:
            tx.save(status="rollback_failed", error=type(exc).__name__ + ": " + str(exc))
            raise
        return tx.record


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    promote_parser = subparsers.add_parser("promote")
    promote_parser.add_argument("--target", choices=["O1", "O2"], required=True)
    promote_parser.add_argument("--expected-current-sha", required=True)
    promote_parser.add_argument("--acceptance", type=Path, required=True)
    promote_parser.add_argument("--acceptance-sha256", required=True)
    promote_parser.add_argument("--transaction", required=True)
    recover_parser = subparsers.add_parser("recover")
    recover_parser.add_argument("--target", choices=["O1", "O2"], required=True)
    recover_parser.add_argument("--transaction", required=True)
    rollback_parser = subparsers.add_parser("rollback-committed")
    rollback_parser.add_argument("--target", choices=["O1", "O2"], required=True)
    rollback_parser.add_argument("--transaction", required=True)
    rollback_parser.add_argument("--rollback-transaction", required=True)
    rollback_parser.add_argument("--auth-overlay-sha256", required=True)
    args = parser.parse_args()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(Refused("deployment interrupted")))

    if args.command == "recover":
        external_controller_guard()
        record = recover(rtx.load_peer(args.target), args.transaction)
        report = {"status": record["status"], "transaction": args.transaction}
    elif args.command == "rollback-committed":
        record = rollback_committed(
            rtx.load_peer(args.target),
            args.transaction,
            args.rollback_transaction,
            args.auth_overlay_sha256,
        )
        report = {
            "status": record["status"],
            "transaction": args.rollback_transaction,
            "source_transaction": args.transaction,
        }
    else:
        record = promote(
            rtx.load_peer(args.target),
            args.expected_current_sha,
            args.acceptance,
            args.acceptance_sha256,
            args.transaction,
        )
        report = {"status": record["status"], "transaction": args.transaction}
    print(json.dumps(report))


if __name__ == "__main__":
    main()
