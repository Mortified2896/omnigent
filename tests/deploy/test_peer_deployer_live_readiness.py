"""Focused regression tests for live peer-promotion layouts."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from deploy.scripts.peer_deployer import host_promotion, identity, transaction


def _target(tmp_path: Path, name: str) -> identity.Instance:
    return identity.Instance(
        name=name,
        deployment_root=tmp_path / name.lower(),
        service_unit=f"{name.lower()}.service",
        host_unit=f"{name.lower()}-host.service",
        port=12001 if name == "O1" else 12002,
        health_url=f"http://127.0.0.1:{12001 if name == 'O1' else 12002}/health",
    )


def _record(
    target: identity.Instance, old_venv: Path, candidate: Path
) -> transaction.TransactionRecord:
    return transaction.TransactionRecord(
        tx_id="promotion-20260809T101112Z-0123abcd",
        target=target.name,
        supervisor="O2" if target.name == "O1" else "O1",
        target_artifact_sha="a" * 40,
        target_artifact_version="1.2.3",
        main_wheel_sha256="1" * 64,
        sdk_client_wheel_sha256="2" * 64,
        sdk_ui_wheel_sha256="3" * 64,
        created_at_unix=1.0,
        old_runtime_path=str(old_venv),
        new_runtime_path=str(candidate),
    )


@pytest.mark.parametrize("name", ["O1", "O2"])
def test_switch_and_rollback_preserve_each_live_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str
) -> None:
    systemd = tmp_path / "systemd"
    monkeypatch.setattr(host_promotion, "SYSTEMD_UNIT_ROOT", systemd)
    target = _target(tmp_path, name)
    root = target.deployment_root
    old_release = root / "releases" / ("b" * 40)
    old_venv = old_release / "venv"
    old_venv.mkdir(parents=True)
    candidate = root / "releases" / ("a" * 40)
    (candidate / "venv").mkdir(parents=True)
    (candidate / "web-ui").mkdir()
    (candidate / "web-ui" / "index.html").write_text("<html></html>")
    (root / "venv").symlink_to(old_venv)
    old_current_target = f"releases/{'b' * 40}/venv" if name == "O1" else f"releases/{'b' * 40}"
    (root / "current").symlink_to(old_current_target)
    (root / "PROVENANCE.txt").write_text("old provenance\n")
    (root / "DEPLOYED_SHA").write_text("old-sha\n")
    conf = host_promotion._web_ui_conf(target)
    conf.parent.mkdir(parents=True)
    old_conf = b'[Service]\nEnvironment="OMNIGENT_WEB_UI_DIST=/old/ui"\n'
    conf.write_bytes(old_conf)
    conf.chmod(0o640)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    metadata = host_promotion._metadata_snapshot(target, evidence)
    record = _record(target, old_venv, candidate)
    accepted = SimpleNamespace(
        runtime_venv_path=str(candidate / "venv"),
        immutable_release_root=str(candidate),
        source_sha="a" * 40,
    )

    host_promotion._switch(
        record,
        target=target,
        candidate=candidate,
        candidate_preexisting=True,
        layout="symlink",
        accepted=accepted,
    )

    assert (root / "venv").resolve() == candidate / "venv"
    expected_current = candidate / "venv" if name == "O1" else candidate
    assert (root / "current").resolve() == expected_current
    assert str(candidate / "web-ui") in conf.read_text()

    host_promotion._restore_runtime(record, target=target, accepted=accepted, layout="symlink")
    host_promotion._restore_metadata(target, evidence, metadata)

    assert (root / "venv").resolve() == old_venv
    assert os.readlink(root / "current") == old_current_target
    assert conf.read_bytes() == old_conf
    assert conf.stat().st_mode & 0o777 == 0o640
    assert (root / "PROVENANCE.txt").read_text() == "old provenance\n"
    assert (root / "DEPLOYED_SHA").read_text() == "old-sha\n"


def test_rollback_metadata_refuses_string_boolean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(host_promotion, "SYSTEMD_UNIT_ROOT", tmp_path / "systemd")
    target = _target(tmp_path, "O1")
    target.deployment_root.mkdir()
    (target.deployment_root / "current").symlink_to("old")
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    state = host_promotion._metadata_snapshot(target, evidence)
    state["web_ui_conf"]["existed"] = "false"

    with pytest.raises(host_promotion.PromotionError, match="must be boolean"):
        host_promotion._restore_metadata(target, evidence, state)


def test_db_rollback_preserves_original_ownership_and_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path, "O1")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setitem(identity.HOME_MAPPING, str(target.deployment_root), home)
    database = home / "chat.db"
    backup = tmp_path / "backup.db"
    for path, version in ((database, "old"), (backup, "backup")):
        with sqlite3.connect(path) as connection:
            connection.execute("CREATE TABLE alembic_version (version_num TEXT)")
            connection.execute("INSERT INTO alembic_version VALUES (?)", (version,))
    database.chmod(0o640)
    observed: list[tuple[int, int]] = []
    monkeypatch.setattr(
        host_promotion.os,
        "chown",
        lambda _path, uid, gid: observed.append((uid, gid)),
    )
    original = database.stat()

    host_promotion._restore_db(target, backup, host_promotion._sha(backup))

    assert observed == [(original.st_uid, original.st_gid)]
    assert database.stat().st_mode & 0o777 == 0o640
    assert host_promotion._db(database)[1] == "backup"


def test_start_target_reloads_systemd_before_service_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path, "O1")
    calls: list[tuple[str, ...]] = []
    monkeypatch.setattr(
        host_promotion,
        "_run",
        lambda argv, **_kwargs: calls.append(tuple(argv)) or SimpleNamespace(returncode=0),
    )
    monkeypatch.setattr(
        host_promotion,
        "_svc",
        lambda unit, *args, **_kwargs: (
            calls.append(("svc", unit, *args)) or SimpleNamespace(returncode=0)
        ),
    )
    monkeypatch.setattr(host_promotion, "_wait_health", lambda _url: None)
    monkeypatch.setattr(host_promotion, "_active", lambda _unit: True)

    host_promotion._start_target(target)

    assert calls[0] == ("systemctl", "daemon-reload")
    assert calls[1] == ("svc", target.service_unit, "start")


def test_live_web_acceptance_fetches_html_and_all_js_css(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = _target(tmp_path, "O2")
    requested: list[str] = []

    def fake_run(argv: list[str], **_kwargs):
        url = argv[-1]
        requested.append(url)
        body = (
            '<html><head><link rel="stylesheet" href="/assets/app.css"></head>'
            '<body><script src="/assets/app.js"></script></body></html>'
            if url.endswith("/")
            else "asset"
        )
        return SimpleNamespace(stdout=body, returncode=0)

    monkeypatch.setattr(host_promotion, "_run", fake_run)

    assert host_promotion._accept_web_ui(target) == 2
    assert requested == [
        f"http://127.0.0.1:{target.port}/",
        f"http://127.0.0.1:{target.port}/assets/app.css",
        f"http://127.0.0.1:{target.port}/assets/app.js",
    ]
