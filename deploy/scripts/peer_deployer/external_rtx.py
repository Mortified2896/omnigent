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
from pathlib import Path
from urllib.request import urlopen

from . import rtx
from .rtx_contract import Peer, Refused, durable_json, require

_TX_ID = re.compile(r"external-[a-z0-9][a-z0-9-]{7,70}")
_SHA = re.compile(r"[a-f0-9]{40}")
_SHA256 = re.compile(r"[a-f0-9]{64}")
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
    args = parser.parse_args()

    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(Refused("deployment interrupted")))

    if args.command == "recover":
        external_controller_guard()
        record = recover(rtx.load_peer(args.target), args.transaction)
    else:
        record = promote(
            rtx.load_peer(args.target),
            args.expected_current_sha,
            args.acceptance,
            args.acceptance_sha256,
            args.transaction,
        )
    print(json.dumps({"status": record["status"], "transaction": args.transaction}))


if __name__ == "__main__":
    main()
