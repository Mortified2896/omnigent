"""Cold state failures preserve both active state and recovery archives."""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
import tarfile

import pytest

from deploy.scripts.peer_deployer import cold_state as m


@pytest.fixture
def bundle(tmp_path):
    db = tmp_path / "chat.db"
    with sqlite3.connect(db) as connection:
        connection.execute("CREATE TABLE alembic_version(version_num TEXT)")
        connection.execute("INSERT INTO alembic_version VALUES ('schema')")
        connection.execute("CREATE TABLE conversations(id TEXT)")
        connection.execute("INSERT INTO conversations VALUES ('accepted-work')")
    archive = tmp_path / "backup.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(db, arcname="state/chat.db")
    spec = {
        "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
        "application_sha": "a" * 40,
        "source_checkpoint": "frozen-source",
        "destination_checkpoint": "candidate-preserved",
        "members": {"state/chat.db": {"schema": "schema"}},
    }
    return archive, tmp_path / "stage", spec, db


def test_validated_pair_and_repeat_are_identical(bundle):
    archive, root, spec, active = bundle
    before = active.read_bytes()
    first = m.stage(archive, root, spec)
    assert first["phase"] == "validated"
    assert m.stage(archive, root, spec) == first
    assert active.read_bytes() == before


def test_bad_archive_never_creates_staging(bundle):
    archive, root, spec, _active = bundle
    spec["archive_sha256"] = "0" * 64
    with pytest.raises(m.ColdStateError, match="checksum"):
        m.stage(archive, root, spec)
    assert not root.exists()


def test_validation_failure_preserves_pair_and_active_data(bundle):
    archive, root, spec, active = bundle
    before = active.read_bytes()
    spec["members"]["state/chat.db"]["schema"] = "incompatible"
    with pytest.raises(m.ColdStateError, match="schema"):
        m.stage(archive, root, spec)
    assert archive.is_file() and (root / "files/state/chat.db").is_file()
    assert active.read_bytes() == before
    assert json.loads((root / "checkpoint.json").read_text())["phase"] == "interrupted-or-invalid"


def test_interrupted_copy_resumes_and_retains_partial(bundle, monkeypatch):
    archive, root, spec, _active = bundle
    original = m.shutil.copyfileobj

    def interrupt(source, destination, **kwargs):
        destination.write(source.read(50))
        raise OSError("connection interrupted")

    monkeypatch.setattr(m.shutil, "copyfileobj", interrupt)
    with pytest.raises(OSError):
        m.stage(archive, root, spec)
    monkeypatch.setattr(m.shutil, "copyfileobj", original)
    result = m.stage(archive, root, spec)
    assert result["phase"] == "validated" and len(result["interrupted"]) == 1
    assert (root / result["interrupted"][0]).stat().st_size == 50


def test_changed_checkpoint_and_newer_work_never_overwritten(bundle):
    archive, root, spec, _active = bundle
    m.stage(archive, root, spec)
    changed = copy.deepcopy(spec)
    changed["destination_checkpoint"] = "new-user-work"
    with pytest.raises(m.ColdStateError, match="identity"):
        m.stage(archive, root, changed)
    target = root / "files/state/chat.db"
    target.write_bytes(b"newly accepted work")
    with pytest.raises(m.ColdStateError, match="changed"):
        m.stage(archive, root, spec)
    assert target.read_bytes() == b"newly accepted work"


def test_recovery_is_a_new_isolated_pair(bundle):
    archive, root, spec, _active = bundle
    first = m.stage(archive, root, spec)
    recovery = root.with_name("recovery-rehearsal")
    second = m.stage(archive, recovery, spec)
    assert first["spec"]["application_sha"] == second["spec"]["application_sha"]
    assert (root / "files/state/chat.db").read_bytes() == (
        recovery / "files/state/chat.db"
    ).read_bytes()


@pytest.mark.parametrize("member", ["../active/chat.db", "/active/chat.db", "state/../chat.db"])
def test_archive_traversal_refused(bundle, member):
    archive, root, spec, _active = bundle
    spec["members"] = {member: {}}
    with pytest.raises(m.ColdStateError):
        m.stage(archive, root, spec)
    assert not root.exists()
