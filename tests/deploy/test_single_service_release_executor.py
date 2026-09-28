"""Disposable controller tests over the real Omnigent SQLite schema."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from deploy.scripts.release_controller import core
from deploy.scripts.release_controller.executor import (
    AcceptedRelease,
    ActivationBusy,
    DisposableExecutor,
    DisposableLayout,
    ExecutorError,
)
from omnigent.db.utils import get_or_create_engine
from omnigent.deployment_quiescence import ComponentObservation, QuiescenceCertificate
from omnigent.entities import MessageData, NewConversationItem
from omnigent.server.deployment_quiescence import persistent_tree_digest
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

SHA_A = "a" * 40
SHA_B = "b" * 40
PACKAGE_VERSION = "0.13.0"
NOW = 1_800_000_000.0


@dataclass(frozen=True)
class ReleaseAssets:
    root: Path
    release_root: Path
    acceptance_root: Path
    schema: str
    conversation_id: str


def _clear_engine(uri: str) -> None:
    from omnigent.db import utils

    engine = utils._engine_cache.pop(uri, None)
    if engine is not None:
        engine.dispose()


@pytest.fixture(scope="module")
def assets(tmp_path_factory: pytest.TempPathFactory) -> ReleaseAssets:
    root = tmp_path_factory.mktemp("single-service-assets")
    template_db = root / "schema-template.db"
    uri = f"sqlite:///{template_db}"
    engine = get_or_create_engine(uri)
    store = SqlAlchemyConversationStore(uri)
    conversation = store.create_conversation(title="saved before activation")
    store.append(
        conversation.id,
        [
            NewConversationItem(
                type="message",
                response_id="disposable-response",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "saved conversation"}],
                ),
            )
        ],
    )
    with engine.connect() as connection:
        schema = str(
            connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalar()
        )
        assert schema
    with sqlite3.connect(template_db) as connection:
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    _clear_engine(uri)

    releases = root / "releases"
    artifacts = root / "artifacts"
    releases.mkdir()
    artifacts.mkdir()
    for sha in (SHA_A, SHA_B):
        _create_accepted_release(releases, artifacts, sha, schema)
    releases.chmod(0o555)
    artifacts.chmod(0o555)
    return ReleaseAssets(root, releases, artifacts, schema, conversation.id)


def _create_accepted_release(
    releases: Path,
    artifacts: Path,
    source_sha: str,
    schema: str,
) -> None:
    release = releases / source_sha
    (release / "artifacts").mkdir(parents=True)
    web = release / "web"
    (web / "assets").mkdir(parents=True)
    (web / "index.html").write_text(
        '<html><script src="/assets/app.js"></script>'
        '<link rel="stylesheet" href="/assets/app.css"></html>'
    )
    (web / "assets" / "app.js").write_text("window.disposable = true;\n")
    (web / "assets" / "app.css").write_text("body { color: #111; }\n")

    venv = release / "venv"
    bin_dir = venv / "bin"
    site = (
        venv / "lib" / f"python{sys.version_info.major}.{sys.version_info.minor}" / "site-packages"
    )
    bin_dir.mkdir(parents=True)
    for module, distribution in (
        ("omnigent", "omnigent"),
        ("omnigent_client", "omnigent-client"),
        ("omnigent_ui_sdk", "omnigent-ui-sdk"),
    ):
        module_dir = site / module
        module_dir.mkdir(parents=True)
        (module_dir / "__init__.py").write_text("\n")
        if module == "omnigent":
            (module_dir / "_build_info.py").write_text(f"COMMIT_SHA = '{source_sha}'\n")
        # Wheel install metadata directories use underscores for normalized
        # distribution names (the way uv/pip actually lays them out).
        metadata = site / f"{distribution.replace('-', '_')}-{PACKAGE_VERSION}.dist-info"
        metadata.mkdir()
        (metadata / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: {PACKAGE_VERSION}\n"
        )
    interpreter = bin_dir / "python"
    shutil.copy2(sys.executable, interpreter)
    interpreter.chmod(0o555)
    (venv / "pyvenv.cfg").write_text(
        f"home = {Path(sys.executable).parent}\n"
        "include-system-site-packages = false\n"
        f"version = {sys.version.split()[0]}\n"
    )

    hashes: dict[str, str] = {}
    for role, filename in (
        ("main", "omnigent.whl"),
        ("sdk_client", "omnigent_client.whl"),
        ("sdk_ui", "omnigent_ui_sdk.whl"),
    ):
        path = release / "artifacts" / filename
        path.write_bytes(f"{source_sha}:{role}".encode())
        hashes[path.relative_to(release).as_posix()] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    for path in web.rglob("*"):
        if path.is_file():
            hashes[path.relative_to(release).as_posix()] = hashlib.sha256(
                path.read_bytes()
            ).hexdigest()
    _make_immutable_tree(release)
    record = {
        "accepted_at": "2026-09-23T00:00:00Z",
        "checks": {
            "build_identity": True,
            "dependencies": True,
            "frontend": True,
            "isolated_boot": True,
            "o3_off": True,
            "smart_routing": True,
        },
        "controller": "disposable-test-builder",
        "hashes": hashes,
        "info": {
            "build_sha": source_sha,
            "server_version": PACKAGE_VERSION,
        },
        "lineage": [source_sha],
        "package_version": PACKAGE_VERSION,
        "python": str(venv / "bin/python"),
        "runtime": str(release),
        "schema": schema,
        "schema_policy": "same-schema",
        "source_sha": source_sha,
    }
    record_path = artifacts / source_sha / "acceptance-v2.json"
    record_path.parent.mkdir(parents=True)
    record_path.write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    record_path.chmod(0o444)
    _make_immutable_tree(artifacts / source_sha)
    _make_immutable_tree(release)


def _make_immutable_tree(root: Path) -> None:
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_dir():
            path.chmod(0o555)
        elif path.is_file():
            path.chmod(0o555 if path.name == "python" else 0o444)
    root.chmod(0o555)


class FakeRuntime:
    """External adapter simulator; state files remain real migrated SQLite."""

    def __init__(
        self,
        layout: DisposableLayout,
        initial: AcceptedRelease,
        conversation_id: str,
    ) -> None:
        self.layout = layout
        self.release = initial
        self.conversation_id = conversation_id
        self.process_generation = "pid-7001:start-100"
        self.state_identity = "disposable-state-identity"
        self._activity_generation = 0
        self._fence_generation = 0
        self._active_work = 0
        self.active_samples: deque[int] = deque()
        self.fenced = False
        self.running = True
        self.operations: list[str] = []
        self.verify_observations: list[tuple[str, bool]] = []
        self.fail_boot = False
        self.fail_candidate_verify: str | None = None
        self.fail_rollback_verify = False
        self.candidate_started = False
        self.on_first_idle: Any = None
        self._idle_hook_used = False
        self.mutate_after_certificate = False

    def readiness(self) -> core.ControllerReadiness:
        return core.ControllerReadiness(
            independent=True,
            serialized=True,
            drain_ready=True,
            write_fence_ready=True,
            backup_ready=True,
            restart_ready=True,
            rollback_ready=True,
            previous_release_retained=True,
        )

    @property
    def active_work(self) -> int:
        return self._active_work

    @active_work.setter
    def active_work(self, value: int) -> None:
        if self._active_work != value:
            self._activity_generation += 1
        self._active_work = value

    def observe(self) -> core.RuntimeObservation:
        if not self.running:
            raise ExecutorError("disposable Omnigent process is not running")
        if self.active_samples:
            prior = self.active_work
            self.active_work = self.active_samples.popleft()
            if prior and self.active_work == 0 and not self._idle_hook_used:
                self._idle_hook_used = True
                if self.on_first_idle is not None:
                    self.on_first_idle()
        return core.RuntimeObservation(
            release=self.release.identity,
            process_generation=self.process_generation,
            state_identity=self.state_identity,
            state_generation=_state_generation(self.layout.state_root),
            observed_at=NOW,
            healthy=True,
            active_work=self.active_work,
            writes_fenced=self.fenced,
        )

    def writes_are_fenced(self) -> bool:
        return self.fenced

    def fence_writes(self) -> None:
        self.operations.append("fence")
        if not self.fenced:
            self._fence_generation += 1
            self._activity_generation += 1
        self.fenced = True

    def open_writes(self) -> None:
        self.operations.append("open")
        self._activity_generation += 1
        self.fenced = False

    def quiescence_certificate(self) -> QuiescenceCertificate:
        observed = self.observe()
        if not self.fenced or self.active_work != 0:
            raise RuntimeError("fake runtime is not quiescent")
        certificate = QuiescenceCertificate(
            certificate_id=str(uuid4()),
            fence_generation=self._fence_generation,
            process_generation=self.process_generation,
            activity_generation=self._activity_generation,
            state_identity=self.state_identity,
            state_generation=str(observed.state_generation),
            persistent_state_digest=persistent_tree_digest(self.layout.state_root),
            observed_at=NOW,
            components=(
                ComponentObservation("admitted_work", str(self._activity_generation), 0, "known"),
                ComponentObservation("remote_writers", "none", 0, "known"),
            ),
        )
        if self.mutate_after_certificate:
            self._activity_generation += 1
        return certificate

    def verify_quiescence(self, certificate: QuiescenceCertificate) -> None:
        observed = self.observe()
        if (
            not self.fenced
            or self.active_work != 0
            or certificate.fence_generation != self._fence_generation
            or certificate.process_generation != self.process_generation
            or certificate.activity_generation != self._activity_generation
            or certificate.state_identity != self.state_identity
            or certificate.state_generation != observed.state_generation
            or certificate.persistent_state_digest
            != persistent_tree_digest(self.layout.state_root)
        ):
            raise RuntimeError("fake runtime quiescence evidence changed")

    def is_running(self) -> bool:
        return self.running

    def stop(self) -> None:
        self.operations.append("stop")
        self.running = False

    def start(self, release: AcceptedRelease) -> None:
        self.operations.append(f"start:{release.identity.source_sha}")
        if release.identity.source_sha == SHA_B and self.fail_boot:
            raise RuntimeError("candidate boot failed")
        self.release = release
        self.running = True
        self.process_generation = f"pid-{7000 + len(self.operations)}:start-{len(self.operations)}"
        if release.identity.source_sha == SHA_B:
            self.candidate_started = True

    def verify(self, release: AcceptedRelease) -> None:
        self.verify_observations.append((str(release.identity.source_sha), self.fenced))
        if not self.running or self.release.identity != release.identity:
            raise RuntimeError("health/build/acceptance identity mismatch")
        if release.identity.source_sha == SHA_B and self.fail_candidate_verify:
            raise RuntimeError(f"candidate {self.fail_candidate_verify} check failed")
        if (
            release.identity.source_sha == SHA_A
            and self.candidate_started
            and self.fail_rollback_verify
        ):
            raise RuntimeError("rollback verification failed")
        if release.identity.source_sha != SHA_A and release.identity.source_sha != SHA_B:
            raise RuntimeError("unexpected release")


@pytest.fixture
def world(
    tmp_path: Path, assets: ReleaseAssets
) -> tuple[DisposableLayout, FakeRuntime, DisposableExecutor]:
    layout = DisposableLayout(
        root=tmp_path / "single-service",
        release_root_override=assets.release_root,
        acceptance_root_override=assets.acceptance_root,
    )
    layout.initialize()
    shutil.copy2(assets.root / "schema-template.db", layout.state_root / "chat.db")
    releases = AcceptedReleaseCatalogForTests(layout)
    current = releases.load(SHA_A)
    layout.current.symlink_to(current.root)
    runtime = FakeRuntime(layout, current, assets.conversation_id)
    executor = DisposableExecutor(
        layout,
        runtime,
        owner_uid=os.getuid(),
        run_uv_check=False,
        now=lambda: NOW,
        sleep=lambda duration: time.sleep(min(duration, 0.001)),
        drain_timeout=1.0,
        poll_interval=0.001,
    )
    return layout, runtime, executor


class AcceptedReleaseCatalogForTests:
    def __init__(self, layout: DisposableLayout) -> None:
        from deploy.scripts.release_controller.executor import AcceptedReleaseCatalog

        self._catalog = AcceptedReleaseCatalog(
            layout,
            owner_uid=os.getuid(),
            run_uv_check=False,
        )

    def load(self, sha: str) -> AcceptedRelease:
        return self._catalog.load(sha)


def _state_generation(state_root: Path) -> str:
    return persistent_tree_digest(state_root)


def _conversation_store(layout: DisposableLayout) -> SqlAlchemyConversationStore:
    uri = f"sqlite:///{layout.state_root / 'chat.db'}"
    return SqlAlchemyConversationStore(uri)


def _append_item(layout: DisposableLayout, conversation_id: str, content: str) -> None:
    _conversation_store(layout).append(
        conversation_id,
        [
            NewConversationItem(
                type="message",
                response_id=f"response-{content}",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": content}],
                ),
            )
        ],
    )


def _request(executor: DisposableExecutor, candidate: str = SHA_B) -> dict[str, Any]:
    plan = executor.plan(candidate)
    assert plan.status == "ready"
    return plan.request(str(uuid4()))


def _pointer(layout: DisposableLayout, name: str) -> Path:
    return (layout.service_root / name).resolve()


def _conversation_rows(
    database: Path, conversation_id: str
) -> tuple[str, list[tuple[int, int | None]]]:
    encoded_id = bytes.fromhex(conversation_id)
    with sqlite3.connect(database) as connection:
        title = connection.execute(
            "SELECT title FROM conversations WHERE id = ?", (encoded_id,)
        ).fetchone()[0]
        rows = connection.execute(
            "SELECT type, status FROM conversation_items WHERE conversation_id = ? "
            "ORDER BY position",
            (encoded_id,),
        ).fetchall()
    return title, rows


def test_already_current_plan_is_a_noop(world) -> None:
    layout, runtime, executor = world
    before = list(runtime.operations)

    plan = executor.plan(SHA_A)

    assert plan.status == "already_current"
    assert runtime.operations == before
    assert _pointer(layout, "current").name == SHA_A
    assert not (layout.transactions / "activation.lock").exists()


def test_active_work_drains_then_exact_candidate_commits_and_state_survives(world, assets) -> None:
    layout, runtime, executor = world
    request = _request(executor)
    runtime.active_work = 2
    runtime.active_samples.extend([1, 0, 0])
    runtime.on_first_idle = lambda: _append_item(layout, assets.conversation_id, "drained turn")

    transaction = executor.activate(request)

    assert transaction["status"] == "committed"
    assert transaction["phase"] == core.ActivationPhase.WRITES_REOPENED.value
    assert _pointer(layout, "current").name == SHA_B
    assert _pointer(layout, "previous").name == SHA_A
    assert runtime.operations.index("fence") < runtime.operations.index("stop")
    assert runtime.verify_observations[-1] == (SHA_B, True)
    assert runtime.operations[-1] == "open"
    title, rows = _conversation_rows(layout.state_root / "chat.db", assets.conversation_id)
    assert title == "saved before activation"
    assert len(rows) == 2
    assert all(isinstance(item_type, int) for item_type, _status in rows)
    assert all(status is None or isinstance(status, int) for _type, status in rows)
    with sqlite3.connect(layout.state_root / "chat.db") as database:
        assert database.execute("PRAGMA integrity_check").fetchall() == [("ok",)]
        assert (
            database.execute("SELECT version_num FROM alembic_version").fetchone()[0]
            == assets.schema
        )


def test_work_that_never_drains_refuses_without_state_or_pointer_mutation(world) -> None:
    layout, runtime, executor = world
    runtime.active_work = 1
    runtime.active_samples.extend([1] * 100)
    request = _request(executor)
    before_generation = _state_generation(layout.state_root)
    before_ops = list(runtime.operations)

    transaction = executor.activate(request)

    assert transaction["status"] == "refused"
    assert transaction["phase"] == core.ActivationPhase.REFUSED.value
    assert _pointer(layout, "current").name == SHA_A
    assert _state_generation(layout.state_root) == before_generation
    assert "stop" not in runtime.operations[len(before_ops) :]
    assert runtime.fenced is False


def test_changed_quiescence_evidence_prevents_executor_stop(world) -> None:
    """The executor revalidates the server certificate at the stop boundary."""
    layout, runtime, executor = world
    runtime.mutate_after_certificate = True
    transaction = executor.activate(_request(executor))

    assert transaction["status"] == "refused"
    assert "stop" not in runtime.operations
    assert _pointer(layout, "current").name == SHA_A
    assert runtime.fenced is False


@pytest.mark.parametrize("drift", ["process", "state_identity", "state_generation"])
def test_stale_process_or_state_generation_is_rejected_before_mutation(world, drift) -> None:
    _layout, runtime, executor = world
    request = _request(executor)
    if drift == "process":
        runtime.process_generation = "pid-9000:start-900"
    elif drift == "state_identity":
        runtime.state_identity = "different-state"
    else:
        _append_item(runtime.layout, runtime.conversation_id, "stale-state")
    before_ops = list(runtime.operations)

    with pytest.raises(core.ActivationRefused, match="changed"):
        executor.activate(request)

    assert runtime.operations == before_ops
    assert not list(runtime.layout.transactions.glob("*/transaction.json"))


def test_acceptance_digest_drift_is_rejected_before_fencing(world, assets) -> None:
    layout, runtime, executor = world
    request = _request(executor)
    acceptance_path = assets.acceptance_root / SHA_B / "acceptance-v2.json"
    original = acceptance_path.read_bytes()
    try:
        acceptance_path.chmod(0o644)
        acceptance_path.write_bytes(
            original.replace(b"disposable-test-builder", b"mutated-test-builder")
        )
        acceptance_path.chmod(0o444)

        with pytest.raises(core.ActivationRefused, match="changed"):
            executor.activate(request)
    finally:
        acceptance_path.chmod(0o644)
        acceptance_path.write_bytes(original)
        acceptance_path.chmod(0o444)

    assert runtime.operations == []
    assert _pointer(layout, "current").name == SHA_A


def test_candidate_bytes_are_rechecked_before_fencing(world, assets) -> None:
    _layout, runtime, executor = world
    wheel = assets.release_root / SHA_B / "artifacts" / "omnigent.whl"
    original = wheel.read_bytes()
    try:
        wheel.chmod(0o644)
        wheel.write_bytes(original + b"changed")
        wheel.chmod(0o444)

        with pytest.raises(
            ExecutorError,
            match=r"accepted-release bytes failed verification.*digest changed",
        ):
            executor.catalog.load(SHA_B)
    finally:
        wheel.chmod(0o644)
        wheel.write_bytes(original)
        wheel.chmod(0o444)

    assert runtime.operations == []


def test_candidate_embedded_build_sha_is_rechecked_before_fencing(world, assets) -> None:
    _layout, runtime, executor = world
    build_info = (
        assets.release_root
        / SHA_B
        / "venv"
        / "lib"
        / f"python{sys.version_info.major}.{sys.version_info.minor}"
        / "site-packages/omnigent/_build_info.py"
    )
    original = build_info.read_bytes()
    try:
        build_info.chmod(0o644)
        build_info.write_text("COMMIT_SHA = '" + SHA_A + "'\n")
        build_info.chmod(0o444)

        with pytest.raises(ExecutorError, match="embedded build SHA mismatch"):
            executor.catalog.load(SHA_B)
    finally:
        build_info.chmod(0o644)
        build_info.write_bytes(original)
        build_info.chmod(0o444)

    assert runtime.operations == []


@pytest.mark.parametrize("failure", ["boot", "health"])
def test_candidate_boot_or_health_failure_rolls_back_before_reopening(world, failure) -> None:
    layout, runtime, executor = world
    if failure == "boot":
        runtime.fail_boot = True
    else:
        runtime.fail_candidate_verify = "health"
    request = _request(executor)

    transaction = executor.activate(request)

    assert transaction["status"] == "rolled_back"
    assert transaction["rollback_verified"] is True
    assert _pointer(layout, "current").name == SHA_A
    assert runtime.fenced is False
    assert runtime.verify_observations[-1] == (SHA_A, True)
    title, rows = _conversation_rows(layout.state_root / "chat.db", runtime.conversation_id)
    assert title == "saved before activation"
    assert len(rows) == 1


def test_candidate_build_identity_mismatch_rolls_back(world) -> None:
    _layout, runtime, executor = world
    runtime.fail_candidate_verify = "build identity"
    request = _request(executor)

    transaction = executor.activate(request)

    assert transaction["status"] == "rolled_back"
    assert runtime.release.identity.source_sha == SHA_A
    assert transaction["rollback_verified"] is True


class SimulatedControllerInterruption(BaseException):
    """Model process death: normal Exception cleanup must not run."""


@pytest.mark.parametrize("point", ["backed_up", "current_switched"])
def test_interruption_recovery_rolls_back_while_fenced(world, point) -> None:
    layout, runtime, executor = world
    request = _request(executor)
    key = request["idempotency_key"]

    def interrupt(name: str, _record: Any) -> None:
        if name == point:
            raise SimulatedControllerInterruption()

    with pytest.raises(SimulatedControllerInterruption):
        executor.activate(request, failpoint=interrupt)

    before_recovery = _pointer(layout, "current").name
    if point == "backed_up":
        assert before_recovery == SHA_A
    else:
        assert before_recovery == SHA_B
    assert runtime.fenced is True

    recovered = executor.recover(key)

    assert recovered["status"] == "rolled_back"
    assert _pointer(layout, "current").name == SHA_A
    assert runtime.fenced is False
    assert recovered["rollback_verified"] is True


def test_recovery_reconciles_previous_pointer_if_journal_write_was_interrupted(
    world, assets
) -> None:
    layout, runtime, executor = world
    previous = assets.release_root / SHA_B
    layout.previous.symlink_to(previous)
    request = _request(executor)
    key = request["idempotency_key"]

    def interrupt_after_backup(name: str, _record: Any) -> None:
        if name == "backed_up":
            raise SimulatedControllerInterruption()

    with pytest.raises(SimulatedControllerInterruption):
        executor.activate(request, failpoint=interrupt_after_backup)

    # Model power loss after the atomic previous-pointer replace but before
    # the journal could persist previous_switched=true.
    layout.previous.unlink()
    layout.previous.symlink_to(assets.release_root / SHA_A)
    assert runtime.fenced is True

    recovered = executor.recover(key)

    assert recovered["status"] == "rolled_back"
    assert _pointer(layout, "current").name == SHA_A
    assert _pointer(layout, "previous").name == SHA_B
    assert runtime.fenced is False


def test_failed_rollback_marks_recovery_required_and_stays_fenced(world) -> None:
    layout, runtime, executor = world
    runtime.fail_candidate_verify = "health"
    runtime.fail_rollback_verify = True
    request = _request(executor)

    transaction = executor.activate(request)

    assert transaction["status"] == "recovery_required"
    assert transaction["phase"] == core.ActivationPhase.RECOVERY_REQUIRED.value
    assert _pointer(layout, "current").name == SHA_A
    assert runtime.fenced is True
    assert transaction["backup_verified"] is True
    assert transaction["recovery_reason"]


def test_failure_after_writes_reopen_never_restores_stale_state(world, assets) -> None:
    layout, runtime, executor = world
    request = _request(executor)
    before = _state_generation(layout.state_root)

    def fail_after_open(name: str, _record: Any) -> None:
        if name == "writes_reopened":
            _append_item(layout, assets.conversation_id, "written after reopen")
            raise RuntimeError("post-open health monitor failed")

    transaction = executor.activate(request, failpoint=fail_after_open)

    assert transaction["status"] == "recovery_required"
    assert transaction["writes_reopened"] is True
    assert transaction["writes_reopen_started"] is True
    assert _state_generation(layout.state_root) != before
    assert _pointer(layout, "current").name == SHA_B
    assert runtime.fenced is True
    _title, rows = _conversation_rows(layout.state_root / "chat.db", assets.conversation_id)
    assert len(rows) == 2


def test_duplicate_activation_request_is_idempotent(world) -> None:
    _layout, runtime, executor = world
    request = _request(executor)
    first = executor.activate(request)
    operations = list(runtime.operations)

    second = executor.activate(request)

    assert second == first
    assert runtime.operations == operations


def test_concurrent_activation_attempt_is_serialized(world) -> None:
    _layout, _runtime, executor = world
    first_request = _request(executor)
    second_request = _request(executor)
    at_fence = threading.Event()
    continue_first = threading.Event()
    results: list[dict[str, Any]] = []

    def pause_first(name: str, _record: Any) -> None:
        if name == "fenced":
            at_fence.set()
            assert continue_first.wait(timeout=2)

    thread = threading.Thread(
        target=lambda: results.append(executor.activate(first_request, failpoint=pause_first))
    )
    thread.start()
    assert at_fence.wait(timeout=2)
    try:
        with pytest.raises(ActivationBusy):
            executor.activate(second_request)
    finally:
        continue_first.set()
        thread.join(timeout=5)

    assert not thread.is_alive()
    assert results[0]["status"] == "committed"
