"""The supported RTX peer promotion entrypoint; never runs inside its target."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import signal
import socket
import sqlite3
import subprocess
import tarfile
import time
from contextlib import contextmanager
from pathlib import Path
from urllib.request import urlopen

from .rtx_contract import (
    Journal,
    Peer,
    Refused,
    canonical_digest,
    database_evidence,
    digest,
    distinct,
    require,
)

CONFIG = Path("/etc/omnigent-peers")
TRANSACTIONS = Path("/srv/omnigent/peer-transactions")
ARTIFACTS = Path("/srv/omnigent/artifacts")


def run(args: list[str], *, timeout: int = 60) -> str:
    result = subprocess.run(
        args,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd="/tmp",
        env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "PYTHONDONTWRITEBYTECODE": "1"},
    )
    require(result.returncode == 0, f"command failed: {args[0]} (rc={result.returncode})")
    return result.stdout.strip()


def trusted(path: Path) -> None:
    require(path.exists() and not path.is_symlink(), f"missing/indirect trusted file: {path}")
    for item in [path, *path.parents]:
        st = item.stat()
        require(st.st_uid == 0 and not st.st_mode & 0o022, f"untrusted owner/mode: {item}")


def load_peer(name: str) -> Peer:
    require(name in {"O1", "O2"}, "unknown peer")
    path = CONFIG / f"{name.lower()}.json"
    trusted(path)
    peer = Peer.from_dict(json.loads(path.read_text()))
    require(peer.instance == name, "manifest instance mismatch")
    return peer


def info(peer: Peer) -> dict:
    with urlopen(f"http://127.0.0.1:{peer.port}/v1/info", timeout=5) as response:
        return json.load(response)


def process(peer: Peer, unit: str, role: str, expected_sha: str) -> dict:
    values = dict(
        line.split("=", 1)
        for line in run(
            [
                "systemctl",
                "show",
                unit,
                "-p",
                "MainPID",
                "-p",
                "ActiveState",
                "-p",
                "ActiveEnterTimestampMonotonic",
                "-p",
                "ControlGroup",
            ]
        ).splitlines()
    )
    require(values["ActiveState"] == "active" and int(values["MainPID"]) > 1, f"inactive {unit}")
    pid = values["MainPID"]
    proc = Path("/proc") / pid
    environment = dict(
        part.split("=", 1) for part in (proc / "environ").read_text().split("\0") if "=" in part
    )
    expected = {
        "OMNIGENT_INSTANCE_ID": peer.instance,
        "OMNIGENT_DATA_DIR": str(peer.root / "state"),
        "OMNIGENT_CONFIG_HOME": str(peer.root / "config"),
        "HOME": str(peer.root / "home"),
        "OMNIGENT_SESSION_COOKIE_SUFFIX": peer.instance,
        "OMNIGENT_HOST_ID": peer.host_id,
        "OMNIGENT_HOST_NAME": f"rtx-{peer.instance.lower()}",
        "OMNIGENT_STRICT_HOST_IDENTITY": "1",
        "OMNIGENT_O3_ROUTING_REVIEW": "0",
    }
    require(
        all(environment.get(k) == v for k, v in expected.items()),
        f"effective process environment mismatch: {unit}",
    )
    require(not environment.get("PYTHONPATH"), "runtime overlay not accepted")
    command = (proc / "cmdline").read_text().split("\0")[:-1]
    python = peer.current.resolve() / "venv/bin/python"
    require(
        command[:4] == [str(python), "-m", "omnigent.cli", role], f"wrong executable/role: {unit}"
    )
    require(expected_sha in str(python), "wrong process release")
    cgroup = (proc / "cgroup").read_text()
    require(values["ControlGroup"] in cgroup, "process unit ownership mismatch")
    # A duplicate launch with the same identity cannot hide behind manager metadata.
    matches = []
    for other in Path("/proc").iterdir():
        if not other.name.isdigit():
            continue
        try:
            cmd = (other / "cmdline").read_bytes().split(b"\0")
            if len(cmd) < 4 or cmd[1:4] != [b"-m", b"omnigent.cli", role.encode()]:
                continue
            env = (other / "environ").read_bytes().split(b"\0")
            if f"OMNIGENT_INSTANCE_ID={peer.instance}".encode() in env:
                matches.append(other.name)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
    require(matches == [pid], f"duplicate/stale process identity: {unit}")
    return {**values, "executable": str(python), "environment": expected}


def snapshot(peer: Peer, expected_sha: str) -> dict:
    require(peer.current.is_symlink(), "current release pointer missing")
    observed = info(peer)
    require(observed.get("instance_id") == peer.instance, "wrong target instance identity")
    require(observed.get("build_sha") == expected_sha, "wrong expected-current SHA")
    require(observed.get("smart_routing_enabled") is True, "Smart Routing unavailable")
    require(observed.get("o3_routing_review_enabled") is False, "O3 must be disabled")
    database = database_evidence(peer)
    # The supervising task may append messages while the target is stopped.
    database.pop("counts")
    return {
        "server": process(peer, peer.unit, "server", expected_sha),
        "host": process(peer, peer.host_unit, "host", expected_sha),
        "database": database,
        "info": observed,
    }


def accepted(path: Path, expected_digest: str) -> dict:
    trusted(path)
    record = json.loads(path.read_text())
    require(canonical_digest(record) == expected_digest, "accepted-artifact mismatch")
    sha = record["source_sha"]
    require(bool(re.fullmatch(r"[a-f0-9]{40}", sha)), "invalid artifact SHA")
    require(path == ARTIFACTS / sha / "acceptance-v2.json", "wrong acceptance path")
    runtime = Path(record["runtime"])
    require(runtime == Path("/srv/omnigent/releases") / sha, "wrong immutable runtime path")
    trusted(runtime)
    for name, expected in record["hashes"].items():
        relative = Path(name)
        require(not relative.is_absolute() and ".." not in relative.parts, "unsafe artifact path")
        resource = runtime / relative
        trusted(resource)
        require(digest(resource) == expected, f"artifact changed: {name}")
    require(record["schema_policy"] == "same-schema", "unsupported migration policy")
    require(
        all(
            record["checks"].get(k) is True
            for k in (
                "dependencies",
                "isolated_boot",
                "build_identity",
                "frontend",
                "o3_off",
                "smart_routing",
            )
        ),
        "incomplete candidate acceptance",
    )
    python = runtime / "venv/bin/python"
    require(os.access(python, os.X_OK), "candidate executable missing")
    build = run(
        [str(python), "-c", "from omnigent._build_info import COMMIT_SHA; print(COMMIT_SHA)"]
    )
    require(build == sha, "candidate executable build mismatch")
    run(["/srv/tools/uv-0.12.1/bin/uv", "pip", "check", "--python", str(python)])
    return record


@contextmanager
def locked(root: Path, recovering: str | None = None):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "deploy.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise Refused("deployment already in flight") from exc
        for path in root.glob("*/transaction.json"):
            if path.parent.name == recovering:
                continue
            state = json.loads(path.read_text())
            require(
                state["status"] in {"committed", "rolled_back", "refused"},
                f"unresolved transaction requires recovery: {path.parent.name}",
            )
        yield


def switch(peer: Peer, release: Path, tx: Journal) -> None:
    require(str(peer.current) in tx.record["owned"], "unowned release pointer")
    require(tx.record["mutation_boundary"], "switch before mutation boundary")
    require(
        str(peer.current.resolve()) in {tx.record["old_release"], tx.record["accepted_release"]},
        "active release changed outside transaction",
    )
    temp = peer.root / f".current-{tx.path.parent.name}"
    require(not temp.exists() and not temp.is_symlink(), "switch staging collision")
    tx.save(owned=[*tx.record["owned"], str(temp)])
    temp.symlink_to(release)
    os.replace(temp, peer.current)


def stop(peer: Peer) -> None:
    run(["systemctl", "stop", peer.host_unit, peer.unit], timeout=90)
    for unit in (peer.unit, peer.host_unit):
        require(
            run(["systemctl", "show", unit, "-p", "MainPID", "--value"]) == "0",
            "target process survived stop",
        )


def start(peer: Peer, sha: str) -> dict:
    run(["systemctl", "start", peer.unit], timeout=90)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            observed = info(peer)
            if observed.get("build_sha") == sha:
                break
        except (OSError, ValueError):
            pass
        time.sleep(1)
    else:
        raise Refused("bounded target startup failed")
    run(["systemctl", "start", peer.host_unit], timeout=90)
    deadline = time.monotonic() + 45
    while time.monotonic() < deadline:
        try:
            return snapshot(peer, sha)
        except (OSError, Refused):
            time.sleep(1)
    raise Refused("bounded host startup failed")


def backup(peer: Peer, directory: Path) -> dict:
    destination = directory / "chat.db"
    require(not destination.exists(), "backup already exists")
    with (
        sqlite3.connect(f"file:{peer.db}?mode=ro", uri=True) as source,
        sqlite3.connect(destination) as target,
    ):
        source.backup(target)
        require(
            target.execute("PRAGMA integrity_check").fetchall() == [("ok",)],
            "backup integrity failed",
        )
    # Retain non-DB state for recovery, including saved native sessions.
    archive = directory / "state.tar"
    run(
        [
            "tar",
            "--exclude=./chat.db",
            "--exclude=./chat.db-wal",
            "--exclude=./chat.db-shm",
            "-cf",
            str(archive),
            "-C",
            str(peer.root / "state"),
            ".",
        ],
        timeout=180,
    )
    return {
        "path": str(destination),
        "sha256": digest(destination),
        "state_archive": str(archive),
        "state_sha256": digest(archive),
    }


def restore_state(peer: Peer, tx: Journal) -> None:
    saved = tx.record["backup"]
    db = Path(saved["path"])
    archive = Path(saved["state_archive"])
    require(db.parent == tx.path.parent and digest(db) == saved["sha256"], "backup DB mismatch")
    require(
        archive.parent == tx.path.parent and digest(archive) == saved["state_sha256"],
        "backup state mismatch",
    )
    state = peer.root / "state"
    stage = peer.root / f".restore-{tx.path.parent.name}"
    failed = tx.path.parent / "failed-state"
    resources = [str(state), str(stage), str(failed)]
    if "state_restore" not in tx.record:
        require(not stage.exists() and not failed.exists(), "rollback staging collision")
        tx.save(owned=[*tx.record["owned"], *resources], state_restore="preparing")
    tx.authorize_rollback(resources)
    if tx.record["state_restore"] == "preparing":
        if stage.exists():
            require(not stage.is_symlink(), "rollback staging is a symlink")
            shutil.rmtree(stage)
        stage.mkdir(mode=0o700)
        with tarfile.open(archive) as bundle:
            bundle.extractall(stage, filter="data")
        shutil.copy2(db, stage / "chat.db")
        run(["chown", "-R", "hermes:hermes", str(stage)])
        tx.save(state_restore="staged")
    if tx.record["state_restore"] == "staged":
        require(not state.is_symlink() and not failed.is_symlink(), "rollback root is indirect")
        if not failed.exists():
            os.replace(state, failed)
        if stage.exists():
            require(not state.exists(), "unexpected state after rollback rename")
            os.replace(stage, state)
        else:
            require(state.is_dir(), "restored state is missing")
            require(digest(state / "chat.db") == saved["sha256"], "restored DB drift")
        tx.save(state_restore="restored")
    require(tx.record["state_restore"] == "restored", "unknown restore phase")
    database_evidence(peer)


def rollback(peer: Peer, tx: Journal) -> None:
    tx.authorize_rollback([str(peer.current), str(peer.db), peer.unit, peer.host_unit])
    tx.save(status="rolling_back")
    stop(peer)
    if tx.record["database_mutated"]:
        restore_state(peer, tx)
    switch(peer, Path(tx.record["old_release"]), tx)
    evidence = start(peer, tx.record["expected_current_sha"])
    tx.save(status="rolled_back", rollback_evidence=evidence)


def promote(
    target: Peer,
    supervisor: Peer,
    expected_sha: str,
    acceptance_path: Path,
    acceptance_digest: str,
    tx_id: str,
    *,
    legacy_supervisor_sha: str | None = None,
) -> dict:
    distinct(target, supervisor)
    require(os.environ.get("OMNIGENT_INSTANCE_ID") != target.instance, "self-upgrade refused")
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", tx_id)), "invalid transaction ID")
    with locked(TRANSACTIONS):
        candidate = accepted(acceptance_path, acceptance_digest)
        old = snapshot(target, expected_sha)
        if legacy_supervisor_sha is not None:
            require(target.instance == "O2", "legacy supervisor may only update O2")
            require(supervisor == legacy_primary(), "wrong legacy supervisor identity")
            supervisor_sha = legacy_supervisor_sha
        else:
            supervisor_sha = info(supervisor).get("build_sha")
            require(isinstance(supervisor_sha, str), "supervisor build missing")

        def observe_supervisor():
            if legacy_supervisor_sha is not None:
                return legacy_snapshot(supervisor_sha)
            return snapshot(supervisor, supervisor_sha)

        before = observe_supervisor()
        require(old["database"]["schema"] == candidate["schema"], "DB schema mismatch")
        # Both peers must already run a proven baseline; bootstrap is a separate gated operation.
        directory = TRANSACTIONS / tx_id
        directory.mkdir(mode=0o700)
        tx = Journal.create(
            directory / "transaction.json",
            target=target,
            supervisor=supervisor,
            expected=expected_sha,
            old=str(target.current.resolve()),
            accepted=candidate["runtime"],
            artifact_digest=acceptance_digest,
        )
        tx.save(
            supervisor_before=before,
            target_before=old,
            legacy_supervisor_sha=legacy_supervisor_sha,
        )
        try:
            require(observe_supervisor() == before, "supervisor drift")
            require(snapshot(target, expected_sha) == old, "target drift")
            # This durable boundary precedes even the first service stop.
            tx.save(mutation_boundary=True, status="stopping")
            stop(target)
            tx.save(backup=backup(target, directory), status="backed_up")
            accepted(acceptance_path, acceptance_digest)
            switch(target, Path(candidate["runtime"]), tx)
            tx.save(database_mutated=True, status="starting")
            after = start(target, candidate["source_sha"])
            require(observe_supervisor() == before, "supervisor drift")
            tx.save(status="committed", target_after=after)
        except BaseException as exc:
            tx.save(error=type(exc).__name__ + ": " + str(exc))
            if tx.record["mutation_boundary"]:
                rollback(target, tx)
                require(observe_supervisor() == before, "supervisor drift on rollback")
            else:
                tx.save(status="refused")
            raise
        return tx.record


def legacy_primary() -> Peer:
    """Exact temporary supervisor identity during migration, never a v2 peer."""
    if (CONFIG / "o1.json").exists():
        prepared = load_peer("O1")
        require(not prepared.db.exists(), "legacy supervision ends after v2 O1 adoption")
        for unit in (prepared.unit, prepared.host_unit):
            require(
                run(["systemctl", "show", unit, "-p", "MainPID", "--value"]) == "0",
                "v2 O1 is already running",
            )
    return Peer(
        "O1",
        Path("/srv/omnigent/candidate"),
        4098,
        443,
        "0c28633609414e1c9e12314fcbf97d23",
        "legacy-primary-sqlite",
    )


def legacy_snapshot(expected_sha: str) -> dict:
    result = {}
    for unit, role in [
        ("omnigent-candidate.service", "server"),
        ("omnigent-candidate-host.service", "host"),
    ]:
        pid = run(["systemctl", "show", unit, "-p", "MainPID", "--value"])
        require(pid.isdigit() and int(pid) > 1, "legacy O1 is not running")
        proc = Path("/proc") / pid
        env = dict(x.split("=", 1) for x in (proc / "environ").read_text().split("\0") if "=" in x)
        require(
            env.get("RTX_CANDIDATE_SHA") == expected_sha, "legacy expected-current SHA mismatch"
        )
        require(
            env.get("OMNIGENT_DATA_DIR") == "/srv/omnigent/candidate/state", "legacy DB mismatch"
        )
        executable = f"/srv/omnigent/releases/{expected_sha}/venv/bin/python"
        require(
            (proc / "cmdline").read_text().split("\0")[:4]
            == [executable, "-m", "omnigent.cli", role],
            "legacy executable mismatch",
        )
        result[unit] = {"pid": pid, "sha": expected_sha, "executable": executable}
    with urlopen("http://127.0.0.1:4098/health", timeout=5) as response:
        require(response.status == 200, "legacy supervisor unhealthy")
    return result


def adopt_legacy_o1(
    target: Peer,
    supervisor: Peer,
    expected_sha: str,
    acceptance_path: Path,
    acceptance_digest: str,
    tx_id: str,
) -> dict:
    require(
        target.instance == "O1" and supervisor.instance == "O2",
        "adoption requires O2 supervising O1",
    )
    distinct(target, supervisor)
    require(os.environ.get("OMNIGENT_INSTANCE_ID") != "O1", "self-upgrade refused")
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", tx_id)), "invalid transaction ID")
    with locked(TRANSACTIONS):
        candidate = accepted(acceptance_path, acceptance_digest)
        before = snapshot(supervisor, candidate["source_sha"])
        legacy = legacy_snapshot(expected_sha)
        require(not target.db.exists(), "adoption refuses an existing target DB")
        require(
            target.current.resolve() == Path("/srv/omnigent/releases") / expected_sha,
            "prepared O1 old release mismatch",
        )
        directory = TRANSACTIONS / tx_id
        directory.mkdir(mode=0o700)
        tx = Journal.create(
            directory / "transaction.json",
            target=target,
            supervisor=supervisor,
            expected=expected_sha,
            old=str(target.current.resolve()),
            accepted=candidate["runtime"],
            artifact_digest=acceptance_digest,
        )
        tx.save(
            operation="adopt-legacy-primary",
            legacy_before=legacy,
            supervisor_before=before,
            owned=[
                *tx.record["owned"],
                *legacy.keys(),
                "omnigent-candidate-auth.service",
                "omnigent-candidate-auth.timer",
                str(target.root / "state"),
                str(target.root / "home"),
            ],
        )
        try:
            require(legacy_snapshot(expected_sha) == legacy, "legacy O1 drift")
            require(snapshot(supervisor, candidate["source_sha"]) == before, "supervisor drift")
            tx.save(mutation_boundary=True, status="stopping_legacy")
            run(
                [
                    "systemctl",
                    "stop",
                    "omnigent-candidate-auth.timer",
                    "omnigent-candidate-auth.service",
                    "omnigent-candidate-host.service",
                    "omnigent-candidate.service",
                ],
                timeout=90,
            )
            source = Path("/srv/omnigent/candidate")
            for unit in legacy:
                require(
                    run(["systemctl", "show", unit, "-p", "MainPID", "--value"]) == "0",
                    "legacy process survived stop",
                )
            original = source / "state/chat.db"
            saved = directory / "legacy-chat.db"
            with (
                sqlite3.connect(f"file:{original}?mode=ro", uri=True) as db,
                sqlite3.connect(saved) as copy,
            ):
                db.backup(copy)
                require(
                    copy.execute("pragma integrity_check").fetchall() == [("ok",)],
                    "legacy backup integrity",
                )
                require(
                    copy.execute("select version_num from alembic_version").fetchone()[0]
                    == candidate["schema"],
                    "legacy schema mismatch",
                )
            tx.save(backup={"path": str(saved), "sha256": digest(saved)}, status="copying_state")
            shutil.copytree(
                source / "state",
                target.root / "state",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns("chat.db*", "daemons", "cache", "logs"),
            )
            shutil.copytree(
                source / "home",
                target.root / "home",
                dirs_exist_ok=True,
                ignore=shutil.ignore_patterns(".cache"),
            )
            shutil.copy2(saved, target.db)
            with sqlite3.connect(target.db) as db:
                db.execute("create table rtx_instance_identity(instance text, identity text)")
                db.execute(
                    "insert into rtx_instance_identity values (?, ?)",
                    (target.instance, target.database_id),
                )
            run(
                [
                    "chown",
                    "-R",
                    "hermes:hermes",
                    str(target.root / "state"),
                    str(target.root / "home"),
                ]
            )
            database_evidence(target)
            switch(target, Path(candidate["runtime"]), tx)
            after = start(target, candidate["source_sha"])
            require(snapshot(supervisor, candidate["source_sha"]) == before, "supervisor drift")
            tx.save(status="committed", target_after=after)
        except BaseException as exc:
            tx.save(error=str(exc))
            if tx.record["mutation_boundary"]:
                rollback_adoption(target, tx)
            else:
                tx.save(status="refused")
            raise
        return tx.record


def rollback_adoption(target: Peer, tx: Journal) -> None:
    units = [
        "omnigent-candidate.service",
        "omnigent-candidate-host.service",
        "omnigent-candidate-auth.timer",
    ]
    tx.authorize_rollback([target.unit, target.host_unit, str(target.current), *units])
    stop(target)
    switch(target, Path(tx.record["old_release"]), tx)
    run(["systemctl", "start", *units], timeout=90)
    deadline = time.monotonic() + 90
    while time.monotonic() < deadline:
        try:
            evidence = legacy_snapshot(tx.record["expected_current_sha"])
            tx.save(status="rolled_back", rollback_evidence=evidence)
            return
        except (OSError, Refused):
            time.sleep(1)
    raise Refused("legacy rollback startup timed out; preserved both state roots")


def recover(target: Peer, supervisor: Peer, tx_id: str) -> dict:
    distinct(target, supervisor)
    require(bool(re.fullmatch(r"[a-z0-9][a-z0-9-]{7,79}", tx_id)), "invalid transaction ID")
    require(os.environ.get("OMNIGENT_INSTANCE_ID") != target.instance, "self-recovery refused")
    with locked(TRANSACTIONS, recovering=tx_id):
        path = TRANSACTIONS / tx_id / "transaction.json"
        trusted(path)
        record = json.loads(path.read_text())
        require(record["target"] == target.document(), "recovery target identity mismatch")
        require(record["supervisor"] == supervisor.document(), "recovery supervisor mismatch")
        if record.get("legacy_supervisor_sha"):
            require(supervisor == legacy_primary(), "wrong legacy supervisor")
            observed = legacy_snapshot(record["legacy_supervisor_sha"])
        else:
            sha = record["supervisor_before"]["info"]["build_sha"]
            observed = snapshot(supervisor, sha)
        require(observed == record["supervisor_before"], "supervisor drift")
        for key in ("old_release", "accepted_release"):
            release = Path(record[key])
            require(release.parent == Path("/srv/omnigent/releases"), "unowned release path")
            trusted(release)
        tx = Journal(path, record)
        if record.get("operation") == "adopt-legacy-primary":
            rollback_adoption(target, tx)
        else:
            rollback(target, tx)
        return tx.record


def caller_guard(target: Peer) -> None:
    pid = os.getpid()
    seen = set()
    while pid > 1 and pid not in seen:
        seen.add(pid)
        proc = Path("/proc") / str(pid)
        try:
            env = (proc / "environ").read_bytes().split(b"\0")
            cgroup = (proc / "cgroup").read_text()
            require(
                f"OMNIGENT_INSTANCE_ID={target.instance}".encode() not in env,
                "target-owned caller cannot upgrade itself",
            )
            require(
                target.unit not in cgroup and target.host_unit not in cgroup,
                "target cgroup cannot upgrade itself",
            )
            if target.instance == "O1":
                require("omnigent-candidate" not in cgroup, "legacy O1 cannot upgrade itself")
            status = dict(
                line.split(":", 1) for line in (proc / "status").read_text().splitlines()
            )
            pid = int(status["PPid"].strip())
        except (FileNotFoundError, ProcessLookupError):
            break


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", choices=["O1", "O2"], required=True)
    parser.add_argument("--supervisor", choices=["O1", "O2"], required=True)
    parser.add_argument("--expected-current-sha")
    parser.add_argument("--acceptance", type=Path)
    parser.add_argument("--acceptance-sha256")
    parser.add_argument("--recover", action="store_true")
    parser.add_argument("--adopt-legacy-primary", action="store_true")
    parser.add_argument("--legacy-supervisor-sha")
    parser.add_argument("--transaction", required=True)
    args = parser.parse_args()
    require(
        socket.gethostname() == "rtx-omnigent" and os.geteuid() == 0,
        "RTX root controller required",
    )
    require(
        not (args.adopt_legacy_primary and args.legacy_supervisor_sha),
        "conflicting migration modes",
    )
    if args.legacy_supervisor_sha:
        require(
            args.target == "O2" and args.supervisor == "O1", "legacy supervision requires O1 to O2"
        )
        require(
            bool(re.fullmatch(r"[a-f0-9]{40}", args.legacy_supervisor_sha)), "invalid legacy SHA"
        )
        supervisor = legacy_primary()
    else:
        supervisor = load_peer(args.supervisor)
    caller_guard(load_peer(args.target))
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: (_ for _ in ()).throw(Refused("deployment interrupted")))
    if args.recover:
        result = recover(load_peer(args.target), supervisor, args.transaction)
        print(json.dumps({"status": result["status"], "transaction": args.transaction}))
        return
    require(
        bool(args.expected_current_sha and args.acceptance and args.acceptance_sha256),
        "promotion requires expected-current SHA and immutable acceptance identity",
    )
    operation = adopt_legacy_o1 if args.adopt_legacy_primary else promote
    result = operation(
        load_peer(args.target),
        supervisor,
        args.expected_current_sha,
        args.acceptance,
        args.acceptance_sha256,
        args.transaction,
        **(
            {"legacy_supervisor_sha": args.legacy_supervisor_sha}
            if args.legacy_supervisor_sha
            else {}
        ),
    )
    print(json.dumps({"status": result["status"], "transaction": args.transaction}))


if __name__ == "__main__":
    main()
