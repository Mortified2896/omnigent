"""Read-only upgrade checks: SQLite bytes, migration lineage and refusal paths."""

from __future__ import annotations

import importlib.util
import json
import random
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "deploy/scripts/upgrade_016_preflight.py"
_SPEC = importlib.util.spec_from_file_location("upgrade_016_preflight", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
preflight = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(preflight)


def database(path: Path, snapshot_type: str = "TEXT") -> Path:
    assert snapshot_type in {"TEXT", "BLOB"}
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE alembic_version (version_num TEXT NOT NULL)")
        db.execute("INSERT INTO alembic_version VALUES ('c91f6a2d7e40')")
        db.execute("CREATE TABLE preferences (value BLOB)")
        db.execute("CREATE TABLE users (preferences BLOB)")
        db.execute(
            "CREATE TABLE omnigent_conversation_metadata "
            f"(inference_snapshot {snapshot_type})"
        )
        for table in preflight.COUNT_TABLES:
            db.execute(f'CREATE TABLE "{table}" (payload TEXT)')
            db.execute(f'INSERT INTO "{table}" VALUES (?)', ("PRIVATE DATA",))
    return path


def insert(path: Path, table: str, column: str, value: object) -> None:
    with sqlite3.connect(path) as db:
        db.execute(f'INSERT INTO "{table}" ("{column}") VALUES (?)', (value,))


def migration(directory: Path, revision: str, parents: object) -> Path:
    directory.mkdir(exist_ok=True)
    path = directory / f"{revision}_test.py"
    path.write_text(
        f'revision: str = {revision!r}\ndown_revision = {parents!r}\ndepends_on = None\n',
        encoding="utf-8",
    )
    return path


def histories(path: Path, joined: bool = True) -> tuple[Path, Path]:
    target, reference = path / "target", path / "reference"
    migration(target, "ge1b2c3d4e5f", None)
    parent = "ge1b2c3d4e5f"
    for revision in preflight.CUSTOM_REVISIONS:
        source = migration(reference, revision, parent)
        copy = migration(target, revision, parent)
        assert copy.read_bytes() == source.read_bytes()
        parent = revision
    migration(target, "kk1a2b3c4d5e", "ge1b2c3d4e5f")
    migration(target, "ll1a2b3c4d5e", "kk1a2b3c4d5e")
    if joined:
        migration(target, "newmerge016", (parent, "ll1a2b3c4d5e"))
    return target, reference


@pytest.mark.parametrize("value", ["é\x00", b"\x00abc", memoryview(b"abc")])
def test_stored_byte_measurement(value: str | bytes | memoryview) -> None:
    expected = value.encode("utf-8") if isinstance(value, str) else bytes(value)
    assert preflight.stored_bytes(value) == expected


@pytest.mark.parametrize("value", [None, 42, False, ["secret"]])
def test_invalid_value_type_refused(value: object) -> None:
    with pytest.raises(preflight.AuditError):
        preflight.stored_bytes(value)


@pytest.mark.parametrize("size,oversized", [(65_534, 0), (65_535, 0), (65_536, 1)])
def test_preference_threshold_counts_stored_bytes(
    tmp_path: Path, size: int, oversized: int
) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "preferences", "value", b"x" * size)
    before = path.read_bytes()
    result = preflight.audit_database(path)
    assert result["size_checks"]["preferences.value"]["oversized"] == oversized
    assert path.read_bytes() == before
    assert "PRIVATE DATA" not in json.dumps(result)


@pytest.mark.parametrize("value", ["é" * 32_768, "\x00" + "x" * 65_535])
def test_legacy_text_is_not_measured_in_characters(tmp_path: Path, value: str) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "preferences", "value", value)
    result = preflight.audit_database(path)
    assert result["size_checks"]["preferences.value"]["oversized"] == 1


def test_intermediate_users_preferences_is_audited(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "users", "preferences", b"x" * 65_536)
    assert preflight.audit_database(path)["size_checks"]["users.preferences"]["oversized"] == 1


def test_absent_optional_columns_are_not_fabricated_zero_counts(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    with sqlite3.connect(path) as db:
        db.execute("DROP TABLE preferences")
        db.execute("DROP TABLE response_feedback")
    result = preflight.audit_database(path)
    assert result["size_checks"]["preferences.value"] == {"present": False}
    assert result["row_counts"]["response_feedback"] is None


def test_null_values_are_not_losses(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "preferences", "value", None)
    result = preflight.audit_database(path)
    assert result["size_checks"]["preferences.value"]["non_null"] == 0
    assert result["blockers"] == []


def test_non_text_values_are_blockers(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "preferences", "value", 10)
    result = preflight.audit_database(path)
    assert result["size_checks"]["preferences.value"]["unreadable"] == 1
    assert result["blockers"]


def test_custom_advisor_payload_is_not_subject_to_upstream_preference_cap(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "model_advisor_records", "payload", "x" * 100_000)
    result = preflight.audit_database(path)
    assert result["row_counts"]["model_advisor_records"] == 2
    assert result["blockers"] == []


@pytest.mark.parametrize(
    "value,expected", [("{}", 4), (b"{}", 4), (b"\x00\x00{}", 4), ("\x00\x00{}", 6)]
)
def test_small_snapshot_matches_migration_framing(value: str | bytes, expected: int) -> None:
    assert preflight.snapshot_size(value) == expected


def test_corrupt_snapshot_blocks_without_exposing_payload(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "omnigent_conversation_metadata", "inference_snapshot", b"\xffPRIVATE")
    result = preflight.audit_database(path)
    check = result["size_checks"]["omnigent_conversation_metadata.inference_snapshot"]
    assert check["unreadable"] == 1
    assert "PRIVATE" not in json.dumps(result)


def test_scan_budget_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = database(tmp_path / "copy.sqlite")
    monkeypatch.setattr(preflight, "MAX_SCAN_BYTES", 10)
    insert(path, "omnigent_conversation_metadata", "inference_snapshot", "x" * 11)
    result = preflight.audit_database(path)
    check = result["size_checks"]["omnigent_conversation_metadata.inference_snapshot"]
    assert check["unreadable"] == 1


def test_converted_blob_snapshot_is_not_recompressed(tmp_path: Path) -> None:
    path = database(tmp_path / "copy.sqlite", "BLOB")
    insert(
        path, "omnigent_conversation_metadata", "inference_snapshot", b"\x00\x00" + b"x" * 65_534
    )
    result = preflight.audit_database(path)
    check = result["size_checks"]["omnigent_conversation_metadata.inference_snapshot"]
    assert check["measurement"] == "stored"
    assert check["oversized"] == 1


def test_compressible_snapshot_is_not_mistaken_for_loss(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")
    path = database(tmp_path / "copy.sqlite")
    insert(path, "omnigent_conversation_metadata", "inference_snapshot", '"' + "x" * 100_000 + '"')
    result = preflight.audit_database(path)
    check = result["size_checks"]["omnigent_conversation_metadata.inference_snapshot"]
    assert check["oversized"] == 0
    assert check["unreadable"] == 0


def test_incompressible_snapshot_is_flagged(tmp_path: Path) -> None:
    pytest.importorskip("zstandard")
    path = database(tmp_path / "copy.sqlite")
    value = random.Random(0).randbytes(100_000).hex()
    insert(path, "omnigent_conversation_metadata", "inference_snapshot", value)
    result = preflight.audit_database(path)
    check = result["size_checks"]["omnigent_conversation_metadata.inference_snapshot"]
    assert check["oversized"] == 1


def test_compressed_snapshot_decompression_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    zstd = pytest.importorskip("zstandard")
    value = b"\x00\x01" + zstd.ZstdCompressor().compress(b"x" * 10_000)
    monkeypatch.setattr(preflight, "MAX_SCAN_BYTES", 100)
    with pytest.raises(preflight.AuditError, match="decompressed"):
        preflight.snapshot_size(value)


def test_missing_database_is_never_created(tmp_path: Path) -> None:
    path = tmp_path / "missing.sqlite"
    with pytest.raises(FileNotFoundError):
        preflight.audit_database(path)
    assert not path.exists()


def test_non_omnigent_database_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE example (id INTEGER)")
    with pytest.raises(preflight.AuditError, match="Alembic"):
        preflight.audit_database(path)


def test_cli_success_is_not_deployment_acceptance(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = database(tmp_path / "copy.sqlite")
    assert preflight.main(["--database", f"O1={path}"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["checks_passed"] is True
    assert result["deployment_accepted"] is False


def test_cli_data_loss_is_nonzero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = database(tmp_path / "copy.sqlite")
    insert(path, "preferences", "value", b"x" * 65_536)
    assert preflight.main(["--database", f"O1={path}"]) == 2
    assert json.loads(capsys.readouterr().out)["checks_passed"] is False


@pytest.mark.parametrize("alias", ["duplicate", "hardlink", "symlink"])
def test_cli_rejects_shared_o1_o2_copy(tmp_path: Path, alias: str) -> None:
    path = database(tmp_path / "copy.sqlite")
    other = tmp_path / "other.sqlite"
    if alias == "hardlink":
        other.hardlink_to(path)
    elif alias == "symlink":
        other.symlink_to(path)
    else:
        other = path
    assert preflight.main(["--database", f"O1={path}", "--database", f"O2={other}"]) == 2


def test_joined_graph_preserves_custom_migrations(tmp_path: Path) -> None:
    target, reference = histories(tmp_path)
    result = preflight.audit_migrations(target, reference)
    assert result["heads"] == ["newmerge016"]
    assert result["blockers"] == []


def test_fork_and_upstream_heads_must_be_joined(tmp_path: Path) -> None:
    target, reference = histories(tmp_path, joined=False)
    result = preflight.audit_migrations(target, reference)
    assert len(result["heads"]) == 2
    assert any("NEW Alembic merge" in value for value in result["blockers"])


def test_old_migration_rewrite_is_refused(tmp_path: Path) -> None:
    target, reference = histories(tmp_path)
    path = target / "b4d8e2f6a9c1_test.py"
    path.write_text(path.read_text() + "# changed historical file\n")
    result = preflight.audit_migrations(target, reference)
    assert any("rewritten" in value for value in result["blockers"])


def test_missing_originals_never_pass_preservation(tmp_path: Path) -> None:
    target, _ = histories(tmp_path)
    assert preflight.audit_migrations(target)["blockers"]


@pytest.mark.parametrize("case", ["duplicate", "cycle", "missing-parent", "dynamic"])
def test_malformed_graph_is_refused(tmp_path: Path, case: str) -> None:
    target, reference = histories(tmp_path)
    if case == "duplicate":
        (target / "duplicate.py").write_bytes((target / "newmerge016_test.py").read_bytes())
    elif case == "cycle":
        migration(target, "cyclea", "cycleb")
        migration(target, "cycleb", "cyclea")
    elif case == "missing-parent":
        migration(target, "orphan", "notpresent")
    else:
        (target / "dynamic.py").write_text("revision = dangerous()\ndown_revision = None\n")
    with pytest.raises(preflight.AuditError):
        preflight.audit_migrations(target, reference)


def test_graph_audit_never_executes_source(tmp_path: Path) -> None:
    target, reference = histories(tmp_path)
    sentinel = tmp_path / "should-not-exist"
    path = target / "newmerge016_test.py"
    path.write_text(path.read_text() + f"open({str(sentinel)!r}, 'w').write('bad')\n")
    assert preflight.audit_migrations(target, reference)["blockers"] == []
    assert not sentinel.exists()
