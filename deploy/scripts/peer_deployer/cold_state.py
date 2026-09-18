"""Checkpointed extraction of selected cold-backup files into isolated staging.

This module never starts a service, adopts worker identities, or restores an
active database. Archives and interrupted files are always retained.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import shutil
import sqlite3
import tarfile
import uuid
from pathlib import Path
from typing import Any

from .host_promotion import _atomic_json, _sha


class ColdStateError(RuntimeError):
    """An archive, checkpoint, or staged state failed validation."""


def relative_path(value: str) -> Path:
    path = Path(value)
    if path.is_absolute() or ".." in path.parts or not path.parts or str(path) != value:
        raise ColdStateError("non-canonical archive member")
    return path


def below(root: Path, relative: Path) -> Path:
    target = root / relative
    if root.is_symlink() or any(p.is_symlink() for p in (target, *target.parents)):
        raise ColdStateError("staging symlink refused")
    if not target.is_relative_to(root):
        raise ColdStateError("staging path escapes transaction")
    return target


def validate_database(path: Path, schema: str | None) -> dict[str, Any]:
    with path.open("rb") as stream:
        sqlite = stream.read(16) == b"SQLite format 3\x00"
    if not sqlite:
        if schema is not None:
            raise ColdStateError("required database has no SQLite header")
        return {}
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise ColdStateError("database integrity failed")
        tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        revisions = (
            [row[0] for row in db.execute("SELECT version_num FROM alembic_version")]
            if ("alembic_version",) in tables
            else []
        )
        if schema is not None and revisions != [schema]:
            raise ColdStateError("database schema differs from approved application")
    return {"integrity": "ok", "revisions": revisions, "tables": len(tables)}


def stage(archive: Path, root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    """Resume only an identical checkpoint; preserve interrupted member copies."""
    root = root.absolute()
    if any(p.is_symlink() for p in (root, *root.parents)) or ".." in root.parts:
        raise ColdStateError("invalid staging root")
    required = spec.get("members")
    if not isinstance(required, dict) or not required:
        raise ColdStateError("explicit required member map is mandatory")
    for name, item in required.items():
        relative_path(name)
        if not isinstance(item, dict):
            raise ColdStateError("invalid member validation")
    for key in (
        "archive_sha256",
        "application_sha",
        "source_checkpoint",
        "destination_checkpoint",
    ):
        if not isinstance(spec.get(key), str) or not spec[key]:
            raise ColdStateError(f"missing checkpoint binding: {key}")
    if _sha(archive) != spec["archive_sha256"]:
        raise ColdStateError("archive checksum mismatch")
    checkpoint = root / "checkpoint.json"
    record: dict[str, Any]
    if checkpoint.exists():
        record = json.loads(checkpoint.read_text())
        if record["spec"] != spec:
            raise ColdStateError("checkpoint identity changed; use new staging")
    else:
        if root.exists():
            raise ColdStateError("unowned staging directory already exists")
        root.mkdir(parents=True, mode=0o700)
        record = {"spec": spec, "phase": "copying", "completed": {}, "interrupted": []}
        _atomic_json(checkpoint, record)
    if root.is_symlink() or checkpoint.is_symlink():
        raise ColdStateError("checkpoint symlink refused")
    lock_path = below(root, Path(".lock"))
    lock = lock_path.open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    for name, item in record["completed"].items():
        target = below(root, Path("files") / relative_path(name))
        if not target.is_file() or _sha(target) != item["sha256"]:
            raise ColdStateError("completed staging file changed; never overwrite it")
    seen = set()
    try:
        with tarfile.open(archive, "r|gz") as source:
            for member in source:
                if member.name not in required:
                    continue
                if member.name in seen or not member.isfile():
                    raise ColdStateError("duplicate or non-regular required member")
                seen.add(member.name)
                if member.name in record["completed"]:
                    continue
                target = below(root, Path("files") / relative_path(member.name))
                if target.exists():
                    raise ColdStateError("unrecorded completed file; inspect interruption")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                partial = target.with_name(target.name + ".partial")
                if partial.exists():
                    retained = partial.with_name(partial.name + "." + uuid.uuid4().hex)
                    partial.rename(retained)
                    record["interrupted"].append(str(retained.relative_to(root)))
                    _atomic_json(checkpoint, record)
                incoming = source.extractfile(member)
                if incoming is None:
                    raise ColdStateError("required member has no content")
                with incoming, partial.open("xb") as outgoing:
                    shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    outgoing.flush()
                    os.fsync(outgoing.fileno())
                digest = _sha(partial)
                expected = required[member.name].get("sha256")
                if expected is not None and digest != expected:
                    raise ColdStateError("member checksum mismatch")
                partial.rename(target)
                record["completed"][member.name] = {
                    "sha256": digest,
                    "bytes": target.stat().st_size,
                    "archive_mode": member.mode,
                    "archive_uid": member.uid,
                    "archive_gid": member.gid,
                }
                _atomic_json(checkpoint, record)
        if seen != set(required):
            raise ColdStateError("required archive members missing")
        record["phase"] = "validating"
        _atomic_json(checkpoint, record)
        for name, item in required.items():
            target = below(root, Path("files") / relative_path(name))
            record["completed"][name]["database"] = validate_database(target, item.get("schema"))
        record["phase"] = "validated"
        _atomic_json(checkpoint, record)
    except BaseException:
        record["phase"] = "interrupted-or-invalid"
        _atomic_json(checkpoint, record)
        raise
    return record


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--spec", type=Path, required=True)
    parser.add_argument("--staging", type=Path, required=True)
    args = parser.parse_args()
    os.umask(0o077)
    result = stage(args.archive, args.staging, json.loads(args.spec.read_text()))
    print(
        json.dumps(
            {
                "phase": result["phase"],
                "files": len(result["completed"]),
                "staging": str(args.staging),
                "activation": "not performed",
            }
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
