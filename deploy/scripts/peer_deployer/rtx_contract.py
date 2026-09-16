"""RTX deployment identities and durable, explicitly owned transactions."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


class Refused(RuntimeError):
    """An unproven deployment boundary must fail closed."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise Refused(message)


def digest(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def canonical_digest(value: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def durable_json(path: Path, value: dict[str, Any], *, exclusive: bool = False) -> None:
    data = json.dumps(value, sort_keys=True, indent=2) + "\n"
    if exclusive:
        with path.open("x") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    else:
        temp = path.with_suffix(".tmp")
        with temp.open("w") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True)
class Peer:
    instance: str
    root: Path
    port: int
    public_port: int
    host_id: str
    database_id: str

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> Peer:
        peer = cls(**{**value, "root": Path(value["root"])})
        require(peer.instance in {"O1", "O2"}, "unknown instance")
        number = 1 if peer.instance == "O1" else 2
        require(peer.root == Path(f"/srv/omnigent/o{number}"), "wrong instance root")
        require(peer.port == (4097 if number == 1 else 4197), "wrong internal port")
        require(peer.public_port == number * 1111, "wrong public port")
        require(bool(re.fullmatch(r"[a-f0-9]{32}", peer.host_id)), "invalid host ID")
        require(bool(re.fullmatch(r"[a-f0-9]{32}", peer.database_id)), "invalid database ID")
        return peer

    @property
    def unit(self) -> str:
        return f"omnigent-{self.instance.lower()}.service"

    @property
    def host_unit(self) -> str:
        return f"omnigent-{self.instance.lower()}-host.service"

    @property
    def db(self) -> Path:
        return self.root / "state/chat.db"

    @property
    def current(self) -> Path:
        return self.root / "current"

    def document(self) -> dict[str, Any]:
        return {**asdict(self), "root": str(self.root)}


def distinct(target: Peer, supervisor: Peer) -> None:
    for name in ("instance", "root", "port", "public_port", "host_id", "database_id"):
        require(getattr(target, name) != getattr(supervisor, name), f"peers collide on {name}")
    if target.db.exists() and supervisor.db.exists():
        require(not target.db.samefile(supervisor.db), "peers share a database inode")


def database_evidence(peer: Peer) -> dict[str, Any]:
    require(peer.db.is_file() and not peer.db.is_symlink(), "expected durable DB missing")
    require(peer.db.resolve() == peer.db, "DB root contains a symlink")
    with sqlite3.connect(f"file:{peer.db}?mode=ro", uri=True) as connection:
        require(
            connection.execute("PRAGMA integrity_check").fetchall() == [("ok",)], "DB integrity"
        )
        binding = connection.execute(
            "SELECT instance, identity FROM rtx_instance_identity"
        ).fetchall()
        require(binding == [(peer.instance, peer.database_id)], "wrong DB binding or fresh DB")
        schema = connection.execute("SELECT version_num FROM alembic_version").fetchall()
        require(len(schema) == 1, "ambiguous database schema")
        counts = {
            name: connection.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0]
            for name in ("conversations", "conversation_items", "response_feedback")
        }
    return {
        "path": str(peer.db),
        "schema": schema[0][0],
        "counts": counts,
        "identity": binding[0][1],
    }


class Journal:
    """Persist ownership before mutation and retain failed transaction evidence."""

    def __init__(self, path: Path, record: dict[str, Any]):
        self.path, self.record = path, record

    @classmethod
    def create(
        cls,
        path: Path,
        *,
        target: Peer,
        supervisor: Peer,
        expected: str,
        old: str,
        accepted: str,
        artifact_digest: str,
    ) -> Journal:
        distinct(target, supervisor)
        require(bool(re.fullmatch(r"[a-f0-9]{40}", expected)), "expected-current SHA required")
        require(not path.exists(), "transaction replay refused")
        record = {
            "target": target.document(),
            "supervisor": supervisor.document(),
            "expected_current_sha": expected,
            "old_release": old,
            "accepted_release": accepted,
            "acceptance_digest": artifact_digest,
            "mutation_boundary": False,
            "database_mutated": False,
            "status": "preflight",
            "owned": [str(target.current), str(target.db), target.unit, target.host_unit],
            "preserve": [old, accepted, str(supervisor.root)],
            "backup": None,
        }
        durable_json(path, record, exclusive=True)
        return cls(path, record)

    def save(self, **changes: Any) -> None:
        require(
            not (self.record["mutation_boundary"] and changes.get("mutation_boundary") is False),
            "mutation boundary cannot be cleared",
        )
        self.record.update(changes)
        durable_json(self.path, self.record)

    def authorize_rollback(self, resources: list[str]) -> None:
        require(self.record["mutation_boundary"], "rollback before mutation refused")
        require(
            self.record["status"] not in {"committed", "rolled_back"}, "terminal replay refused"
        )
        require(
            set(resources) <= set(self.record["owned"]), "rollback of unowned resource refused"
        )
