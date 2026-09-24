"""Fail-closed write admission and disposable controller evidence.

The controller channel is a fixed Unix-socket protocol. It accepts no path,
command, service name, or browser-supplied evidence. A process with an
unacknowledged host or runner tunnel prevents fencing; disconnecting that
writer does not turn its state into an idle acknowledgement.
"""

from __future__ import annotations

import asyncio
import contextvars
import hashlib
import json
import os
import re
import sqlite3
import stat
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote
from uuid import uuid4

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from omnigent.deployment_quiescence import ComponentObservation, QuiescenceCertificate

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_SAFE_WEBSOCKET_PATHS = frozenset({"/v1/sessions/updates"})
_REMOTE_TUNNEL = re.compile(r"^/v1/(hosts|runners)/([^/]+)/tunnel$")
_CONTROL_MAX_LINE = 4096
_CERTIFICATE_TTL_SECONDS = 5.0


class QuiescenceBlocked(RuntimeError):
    """The requested fence or observation lacks complete zero-work evidence."""

    def __init__(self, blockers: tuple[str, ...]) -> None:
        self.blockers = blockers
        super().__init__(", ".join(blockers))


class AdmissionLease:
    """Reference-counted admission held through the work's actual completion."""

    def __init__(self, coordinator: DeploymentQuiescence, token: str, component: str) -> None:
        self._coordinator = coordinator
        self._token = token
        self.component = component
        self._released = False
        self._release_lock = threading.Lock()

    def fork(self, component: str) -> AdmissionLease | None:
        """Admit derived work from an already-admitted operation, even while fenced."""
        return self._coordinator._fork(self, component)

    def release(self) -> None:
        """Release once; safe from an executor thread's completion callback."""
        with self._release_lock:
            if self._released:
                return
            self._released = True
        self._coordinator._release(self._token)

    @contextmanager
    def activate(self) -> Iterator[AdmissionLease]:
        """Expose this lease to work that explicitly registers derived tasks."""
        token = _CURRENT_ADMISSION.set(self)
        try:
            yield self
        finally:
            _CURRENT_ADMISSION.reset(token)


_CURRENT_ADMISSION: contextvars.ContextVar[AdmissionLease | None] = contextvars.ContextVar(
    "omnigent_deployment_admission", default=None
)


def current_admission_lease() -> AdmissionLease | None:
    """Return the current admitted request/task lease, if one exists."""
    return _CURRENT_ADMISSION.get()


class DeploymentQuiescence:
    """Linearizable process-local admission fence and certificate issuer."""

    def __init__(
        self,
        *,
        required_components: tuple[str, ...] = (),
        start_fenced: bool = False,
    ) -> None:
        self.process_generation = str(uuid4())
        self._lock = threading.RLock()
        # A controller-managed candidate must not accept writes between boot
        # and its health/build/schema checks. Its first process-local fence is
        # generation one and is bound to this fresh process generation.
        self._fence_generation = 1 if start_fenced else 0
        self._activity_generation = 1 if start_fenced else 0
        self._fenced = start_fenced
        self._components_sealed = False
        self._leases: dict[str, str] = {}
        self._external_writers: dict[str, str] = {}
        self._required_components = frozenset(required_components)
        self._providers: dict[str, Callable[[], ComponentObservation]] = {}
        self._certificates: dict[str, tuple[QuiescenceCertificate, float]] = {}

    @property
    def fenced(self) -> bool:
        with self._lock:
            return self._fenced

    @property
    def fence_generation(self) -> int:
        with self._lock:
            return self._fence_generation

    @property
    def activity_generation(self) -> int:
        with self._lock:
            return self._activity_generation

    def register_component(self, name: str, provider: Callable[[], ComponentObservation]) -> None:
        """Register a required, fail-closed runtime work source."""
        with self._lock:
            if self._components_sealed:
                raise RuntimeError("cannot change quiescence components while fenced")
            if name in self._providers:
                raise ValueError(f"quiescence component already registered: {name}")
            self._providers[name] = provider
            self._activity_generation += 1

    def seal_components(self) -> None:
        """Finish startup registration before exposing controller observations."""
        with self._lock:
            self._components_sealed = True

    def try_admit(self, component: str) -> AdmissionLease | None:
        """Atomically admit work or reject it after the fence."""
        with self._lock:
            if self._fenced:
                self._activity_generation += 1
                self._certificates.clear()
                return None
            token = uuid4().hex
            self._leases[token] = component
            self._activity_generation += 1
            self._certificates.clear()
            return AdmissionLease(self, token, component)

    def note_external_writer(self, kind: str, identity: str) -> bool:
        """Remember a remote writer until an explicit drain protocol can ack it.

        The current experiment deliberately has no host/runner drain wire
        protocol. A tunnel is therefore a durable unknown, including after it
        disconnects. Returning ``False`` means a newly connecting writer was
        refused by an already-active fence.
        """
        key = f"{kind}:{identity}:{uuid4().hex}"
        with self._lock:
            if self._fenced:
                self._activity_generation += 1
                self._certificates.clear()
                return False
            self._external_writers[key] = self.process_generation
            self._activity_generation += 1
            self._certificates.clear()
            return True

    def fence(self) -> int:
        """Close this process's admission and start the next drain generation."""
        with self._lock:
            if self._fenced:
                return self._fence_generation
            self._fence_generation += 1
            self._fenced = True
            self._components_sealed = True
            self._activity_generation += 1
            self._certificates.clear()
            return self._fence_generation

    def open_writes(self, generation: int) -> None:
        """Reopen admission only for the exact active fence generation."""
        with self._lock:
            if not self._fenced or generation != self._fence_generation:
                raise QuiescenceBlocked(("fence_generation_changed",))
            self._fenced = False
            self._activity_generation += 1
            self._certificates.clear()

    def issue_certificate(
        self,
        *,
        state_identity: str,
        state_generation: str,
        persistent_state_digest: str,
        observed_at: float | None = None,
    ) -> QuiescenceCertificate:
        """Collect a stable, exact-generation zero-work certificate."""
        with self._lock:
            if not self._components_sealed:
                raise QuiescenceBlocked(("components_not_sealed",))
            if not self._fenced:
                raise QuiescenceBlocked(("writes_not_fenced",))
            fence_generation = self._fence_generation
            activity_generation = self._activity_generation
            process_generation = self.process_generation
            leases = dict(self._leases)
            providers = dict(self._providers)
            external_writers = dict(self._external_writers)

        observations: list[ComponentObservation] = [
            ComponentObservation(
                name="admitted_work",
                generation=str(activity_generation),
                active_work=len(leases),
            ),
            ComponentObservation(
                name="remote_writers",
                generation=hashlib.sha256(
                    json.dumps(external_writers, sort_keys=True).encode()
                ).hexdigest(),
                active_work=None if external_writers else 0,
                status="unknown" if external_writers else "known",
            ),
        ]
        missing = self._required_components - providers.keys()
        if missing:
            observations.extend(
                ComponentObservation(
                    name=name, generation="unknown", active_work=None, status="unknown"
                )
                for name in sorted(missing)
            )
        for name, provider in sorted(providers.items()):
            try:
                observation = provider()
                if observation.name != name:
                    raise ValueError("component provider returned another component name")
            except Exception:  # noqa: BLE001 - unknown runtime state must block.
                observation = ComponentObservation(name, "unknown", None, "unknown")
            observations.append(observation)

        blockers: list[str] = []
        for item in observations:
            if item.status != "known" or type(item.active_work) is not int:
                blockers.append(f"{item.name}_unknown")
            elif item.active_work != 0:
                blockers.append(f"{item.name}_active:{item.active_work}")
        if blockers:
            raise QuiescenceBlocked(tuple(sorted(set(blockers))))

        with self._lock:
            if (
                not self._fenced
                or self._fence_generation != fence_generation
                or self.process_generation != process_generation
                or self._activity_generation != activity_generation
                or self._leases
                or self._external_writers != external_writers
            ):
                raise QuiescenceBlocked(("activity_changed_during_observation",))
            certificate = QuiescenceCertificate(
                certificate_id=str(uuid4()),
                fence_generation=fence_generation,
                process_generation=process_generation,
                activity_generation=activity_generation,
                state_identity=state_identity,
                state_generation=state_generation,
                persistent_state_digest=persistent_state_digest,
                observed_at=time.time() if observed_at is None else observed_at,
                components=tuple(sorted(observations, key=lambda item: item.name)),
            )
            self._certificates[certificate.certificate_id] = (certificate, time.monotonic())
            return certificate

    def verify_certificate(
        self,
        certificate_id: str,
        *,
        state_identity: str,
        state_generation: str,
        now: float | None = None,
    ) -> QuiescenceCertificate:
        """Recollect every component and verify the server-held exact certificate."""
        ref = time.time() if now is None else now
        with self._lock:
            issued = self._certificates.get(certificate_id)
            if issued is None:
                raise QuiescenceBlocked(("certificate_unknown_or_invalidated",))
            certificate, issued_monotonic = issued
            if (
                time.monotonic() - issued_monotonic > _CERTIFICATE_TTL_SECONDS
                or ref < certificate.observed_at
                or ref - certificate.observed_at > _CERTIFICATE_TTL_SECONDS
            ):
                self._certificates.pop(certificate_id, None)
                raise QuiescenceBlocked(("certificate_stale",))
            if not self._fenced:
                raise QuiescenceBlocked(("writes_not_fenced",))
            if certificate.fence_generation != self._fence_generation:
                raise QuiescenceBlocked(("fence_generation_changed",))
            if certificate.process_generation != self.process_generation:
                raise QuiescenceBlocked(("process_generation_changed",))
            if certificate.activity_generation != self._activity_generation or self._leases:
                raise QuiescenceBlocked(("new_or_active_work",))
            if self._external_writers:
                raise QuiescenceBlocked(("remote_writer_unacknowledged",))
            providers = dict(self._providers)
        if certificate.state_identity != state_identity:
            raise QuiescenceBlocked(("state_identity_changed",))
        if certificate.state_generation != state_generation:
            raise QuiescenceBlocked(("state_generation_changed",))

        current: list[ComponentObservation] = []
        for name, provider in sorted(providers.items()):
            try:
                current.append(provider())
            except Exception:  # noqa: BLE001 - unknown state blocks.
                raise QuiescenceBlocked((f"{name}_unknown",)) from None
        expected_by_name = {item.name: item for item in certificate.components}
        if any(expected_by_name.get(item.name) != item for item in current):
            raise QuiescenceBlocked(("component_generation_changed",))

        with self._lock:
            if (
                not self._fenced
                or self._fence_generation != certificate.fence_generation
                or self._activity_generation != certificate.activity_generation
                or self._leases
                or self._external_writers
            ):
                raise QuiescenceBlocked(("activity_changed_during_verification",))
        return certificate

    def _fork(self, parent: AdmissionLease, component: str) -> AdmissionLease | None:
        with self._lock:
            if parent._token not in self._leases:
                return None
            token = uuid4().hex
            self._leases[token] = component
            self._activity_generation += 1
            self._certificates.clear()
            return AdmissionLease(self, token, component)

    def _release(self, token: str) -> None:
        with self._lock:
            if token in self._leases:
                self._leases.pop(token)
                self._activity_generation += 1
                self._certificates.clear()


@dataclass(frozen=True)
class PersistentStateSnapshot:
    """Content generation and inode identity for one disposable state root."""

    identity: str
    generation: str
    content_digest: str


class PersistentStateGeneration:
    """Hash a complete state tree and SQLite commit generations without a schema change."""

    def __init__(self, root: Path, engines: tuple[Any, ...]) -> None:
        if root.is_symlink():
            raise ValueError("deployment state root must be a real directory")
        self.root = root.resolve(strict=True)
        metadata = self.root.stat(follow_symlinks=False)
        if not stat.S_ISDIR(metadata.st_mode) or self.root.is_symlink():
            raise ValueError("deployment state root must be a real directory")
        self._root_identity = (metadata.st_dev, metadata.st_ino)
        self._connections: list[tuple[str, Any]] = []
        seen: set[int] = set()
        try:
            for engine in engines:
                if id(engine) in seen:
                    continue
                seen.add(id(engine))
                if engine.dialect.name != "sqlite":
                    raise ValueError("deployment state generation currently requires SQLite")
                connection = engine.raw_connection()
                cursor = connection.cursor()
                rows = cursor.execute("PRAGMA database_list").fetchall()
                database = next((str(row[2]) for row in rows if row[1] == "main"), "")
                if not database:
                    raise ValueError("in-memory SQLite cannot provide persistent state identity")
                database_path = Path(database).resolve(strict=True)
                try:
                    database_path.relative_to(self.root)
                except ValueError as exc:
                    raise ValueError("a persistent database is outside the state root") from exc
                db_stat = database_path.stat(follow_symlinks=False)
                if not stat.S_ISREG(db_stat.st_mode) or database_path.is_symlink():
                    raise ValueError("persistent database must be a regular in-root file")
                self._connections.append((str(database_path), connection))
            if not self._connections:
                raise ValueError("no persistent SQLite database was bound to the state root")
        except BaseException:
            self.close()
            raise

    def snapshot(self) -> PersistentStateSnapshot:
        """Return a deterministic generation that changes on DB commits or file drift."""
        metadata = self.root.stat(follow_symlinks=False)
        if (metadata.st_dev, metadata.st_ino) != self._root_identity:
            raise ValueError("persistent state root identity changed")
        database_versions: list[tuple[str, int, int, int]] = []
        for database, connection in self._connections:
            cursor = connection.cursor()
            rows = cursor.execute("PRAGMA database_list").fetchall()
            current_path = next((str(row[2]) for row in rows if row[1] == "main"), "")
            resolved = Path(current_path).resolve(strict=True)
            if str(resolved) != database:
                raise ValueError("persistent database path changed")
            db_stat = resolved.stat(follow_symlinks=False)
            data_version = int(cursor.execute("PRAGMA data_version").fetchone()[0])
            database_versions.append((database, db_stat.st_dev, db_stat.st_ino, data_version))
        tree_digest = _persistent_tree_digest(self.root)
        identity = hashlib.sha256(
            json.dumps(
                {"root": str(self.root), "device": metadata.st_dev, "inode": metadata.st_ino},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        generation = hashlib.sha256(
            json.dumps(
                {"databases": database_versions, "tree_sha256": tree_digest},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        return PersistentStateSnapshot(
            identity=identity,
            generation=generation,
            content_digest=tree_digest,
        )

    def close(self) -> None:
        """Return pinned read-only sentinel connections to their SQLAlchemy pools."""
        connections, self._connections = self._connections, []
        for _database, connection in connections:
            connection.close()


def _persistent_tree_digest(root: Path) -> str:
    """Hash regular persistent files; SQLite shared-memory sidecars are transient."""
    entries: list[dict[str, Any]] = []
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        metadata = path.lstat()
        if stat.S_ISLNK(metadata.st_mode):
            raise ValueError(f"persistent state contains a symlink: {relative}")
        if stat.S_ISDIR(metadata.st_mode):
            entries.append({"path": relative, "kind": "dir", "mode": metadata.st_mode & 0o777})
        elif stat.S_ISREG(metadata.st_mode):
            sidecar_base = next(
                (
                    path.with_name(path.name[: -len(suffix)])
                    for suffix in ("-wal", "-shm")
                    if path.name.endswith(suffix)
                ),
                None,
            )
            if (
                sidecar_base is not None
                and sidecar_base.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                and sidecar_base.is_file()
                and not sidecar_base.is_symlink()
            ):
                continue
            if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                digest_value = _sqlite_logical_digest(path)
            else:
                digest = hashlib.sha256()
                with path.open("rb") as stream:
                    before = os.fstat(stream.fileno())
                    if (before.st_dev, before.st_ino) != (metadata.st_dev, metadata.st_ino):
                        raise ValueError(f"persistent state changed while opening: {relative}")
                    for block in iter(lambda: stream.read(1024 * 1024), b""):
                        digest.update(block)
                    after = os.fstat(stream.fileno())
                if (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                ):
                    raise ValueError(f"persistent state changed while hashing: {relative}")
                digest_value = digest.hexdigest()
            entries.append(
                {
                    "path": relative,
                    "kind": "file",
                    "mode": metadata.st_mode & 0o777,
                    "sha256": digest_value,
                    **(
                        {}
                        if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}
                        else {
                            "device": metadata.st_dev,
                            "inode": metadata.st_ino,
                            "size": metadata.st_size,
                            "mtime_ns": metadata.st_mtime_ns,
                            "ctime_ns": metadata.st_ctime_ns,
                        }
                    ),
                }
            )
        else:
            raise ValueError(f"persistent state has an unsupported entry: {relative}")
    return hashlib.sha256(
        json.dumps({"entries": entries}, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def persistent_tree_digest(root: Path) -> str:
    """Return the public state-content digest shared with the disposable executor."""
    return _persistent_tree_digest(root)


def _sqlite_logical_digest(path: Path) -> str:
    """Hash a consistent SQLite snapshot without including mutable WAL sidecars."""
    try:
        uri_path = quote(path.as_posix(), safe="/:")
        with tempfile.TemporaryDirectory(prefix="omnigent-state-digest-") as temporary_root:
            snapshot_path = Path(temporary_root) / "snapshot.sqlite"
            source = sqlite3.connect(f"file:{uri_path}?mode=ro", uri=True)
            target = sqlite3.connect(snapshot_path)
            try:
                source.backup(target)
            finally:
                target.close()
                source.close()
            digest = hashlib.sha256()
            with snapshot_path.open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError(f"persistent SQLite state could not be snapshotted: {path.name}") from exc
    return digest.hexdigest()


class DeploymentFenceMiddleware:
    """Fence mutating HTTP admissions and recheck WS messages after receive."""

    def __init__(self, app: ASGIApp, coordinator: DeploymentQuiescence) -> None:
        self._app = app
        self._coordinator = coordinator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            method = str(scope.get("method", "GET")).upper()
            if method not in _MUTATING_METHODS:
                await self._app(scope, receive, send)
                return
            lease = self._coordinator.try_admit("http_mutator")
            if lease is None:
                response = JSONResponse(
                    status_code=423,
                    content={"detail": "writes are fenced for deployment"},
                )
                await response(scope, receive, send)
                return
            try:
                with lease.activate():
                    await self._app(scope, receive, send)
            finally:
                lease.release()
            return

        if scope["type"] != "websocket":
            await self._app(scope, receive, send)
            return

        path = str(scope.get("path", ""))
        remote = _REMOTE_TUNNEL.fullmatch(path)
        if remote is not None:
            admission = self._coordinator.try_admit("remote_tunnel_handshake")
            if admission is None:
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1013,
                        "reason": "writes are fenced for deployment",
                    }
                )
                return
            handshake_lease: AdmissionLease = admission
            handshake_context = _CURRENT_ADMISSION.set(handshake_lease)
            accepted = False
            refused = False

            def release_handshake() -> None:
                nonlocal handshake_context
                handshake_lease.release()
                if handshake_context is not None:
                    _CURRENT_ADMISSION.reset(handshake_context)
                    handshake_context = None

            async def remote_send(message: Message) -> None:
                nonlocal accepted, refused
                if refused:
                    return
                if message.get("type") == "websocket.accept" and not accepted:
                    if not self._coordinator.note_external_writer(
                        remote.group(1), remote.group(2)
                    ):
                        refused = True
                        await send(
                            {
                                "type": "websocket.close",
                                "code": 1013,
                                "reason": "deployment fence",
                            }
                        )
                        return
                    accepted = True
                    release_handshake()
                await send(message)

            async def remote_receive() -> Message:
                message = await receive()
                if refused:
                    return {"type": "websocket.disconnect", "code": 1013}
                return message

            try:
                await self._app(scope, remote_receive, remote_send)
            finally:
                release_handshake()
            return
        if path in _SAFE_WEBSOCKET_PATHS:
            await self._app(scope, receive, send)
            return

        ws_handshake_lease = self._coordinator.try_admit("websocket_handshake")
        if ws_handshake_lease is None:
            await send(
                {
                    "type": "websocket.close",
                    "code": 1013,
                    "reason": "writes are fenced for deployment",
                }
            )
            return
        current_lease: AdmissionLease | None = ws_handshake_lease
        current_context: contextvars.Token[AdmissionLease | None] | None = _CURRENT_ADMISSION.set(
            ws_handshake_lease
        )
        closed_for_fence = False
        connect_event_seen = False

        async def fenced_receive() -> Message:
            nonlocal current_lease, current_context, closed_for_fence, connect_event_seen
            # Starlette's accept() consumes the initial "websocket.connect"
            # event. Keep the handshake lease through application work that
            # happens before its first client-message receive.
            message: Message | None = None
            if not connect_event_seen:
                message = await receive()
                connect_event_seen = True
                if message.get("type") == "websocket.connect":
                    return message
            if current_lease is not None:
                current_lease.release()
                current_lease = None
            if current_context is not None:
                _CURRENT_ADMISSION.reset(current_context)
                current_context = None
            if message is None:
                message = await receive()
            if message.get("type") != "websocket.receive":
                return message
            lease = self._coordinator.try_admit("websocket_message")
            if lease is None:
                closed_for_fence = True
                await send(
                    {
                        "type": "websocket.close",
                        "code": 1013,
                        "reason": "writes are fenced for deployment",
                    }
                )
                return {"type": "websocket.disconnect", "code": 1013}
            current_lease = lease
            current_context = _CURRENT_ADMISSION.set(lease)
            return message

        async def fenced_send(message: Message) -> None:
            if not closed_for_fence:
                await send(message)

        try:
            await self._app(scope, fenced_receive, fenced_send)
        finally:
            if current_lease is not None:
                current_lease.release()
            if current_context is not None:
                _CURRENT_ADMISSION.reset(current_context)


class DeploymentControlSocket:
    """Fixed local controller channel for fence, observe, verify and reopen."""

    def __init__(
        self,
        path: Path,
        coordinator: DeploymentQuiescence,
        state: PersistentStateGeneration,
    ) -> None:
        self.path = path
        self.coordinator = coordinator
        self.state = state
        self._server: asyncio.AbstractServer | None = None
        self._socket_identity: tuple[int, int] | None = None

    async def start(self) -> None:
        """Start a 0600 Unix-domain control socket without replacing an existing path."""
        if self.path.exists() or self.path.is_symlink():
            raise FileExistsError(f"deployment control socket path already exists: {self.path}")
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.path.parent.is_symlink():
            raise ValueError("deployment control socket parent is indirect")
        self._server = await asyncio.start_unix_server(self._handle, path=str(self.path))
        os.chmod(self.path, 0o600, follow_symlinks=False)
        metadata = self.path.stat(follow_symlinks=False)
        if not stat.S_ISSOCK(metadata.st_mode):
            raise ValueError("deployment control endpoint is not a Unix socket")
        self._socket_identity = (metadata.st_dev, metadata.st_ino)

    async def close(self) -> None:
        """Close the endpoint and remove only the socket inode this process created."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        try:
            metadata = self.path.stat(follow_symlinks=False)
            if stat.S_ISSOCK(metadata.st_mode) and self._socket_identity == (
                metadata.st_dev,
                metadata.st_ino,
            ):
                self.path.unlink()
        except FileNotFoundError:
            pass
        self.state.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), timeout=3.0)
            if not line or len(line) > _CONTROL_MAX_LINE or not line.endswith(b"\n"):
                raise ValueError("invalid control request framing")
            request = json.loads(line)
            if not isinstance(request, dict) or not isinstance(request.get("operation"), str):
                raise ValueError("invalid control request")
            response = await self._dispatch(request)
        except QuiescenceBlocked as exc:
            response = {"ok": False, "blockers": list(exc.blockers)}
        except Exception as exc:  # noqa: BLE001 - protocol errors are returned, never execute input.
            response = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
        writer.write(json.dumps(response, sort_keys=True, separators=(",", ":")).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def _dispatch(self, request: Mapping[str, Any]) -> dict[str, Any]:
        operation = request["operation"]
        if operation == "status" and set(request) == {"operation"}:
            return {
                "ok": True,
                "fenced": self.coordinator.fenced,
                "fence_generation": self.coordinator.fence_generation,
                "process_generation": self.coordinator.process_generation,
            }
        if operation == "fence" and set(request) == {"operation"}:
            return {
                "ok": True,
                "fenced": True,
                "fence_generation": self.coordinator.fence(),
                "process_generation": self.coordinator.process_generation,
            }
        if operation == "observe" and set(request) == {"operation"}:
            snapshot = await asyncio.to_thread(self.state.snapshot)
            certificate = self.coordinator.issue_certificate(
                state_identity=snapshot.identity,
                state_generation=snapshot.generation,
                persistent_state_digest=snapshot.content_digest,
            )
            return {"ok": True, "certificate": certificate.to_dict()}
        if operation == "verify" and set(request) == {"operation", "certificate_id"}:
            certificate_id = request.get("certificate_id")
            if not isinstance(certificate_id, str):
                raise ValueError("certificate_id is required")
            snapshot = await asyncio.to_thread(self.state.snapshot)
            certificate = self.coordinator.verify_certificate(
                certificate_id,
                state_identity=snapshot.identity,
                state_generation=snapshot.generation,
            )
            return {"ok": True, "certificate": certificate.to_dict()}
        if operation == "open_writes" and set(request) == {"operation", "fence_generation"}:
            generation = request.get("fence_generation")
            if type(generation) is not int:
                raise ValueError("fence_generation must be an integer")
            self.coordinator.open_writes(generation)
            return {"ok": True, "fenced": False, "fence_generation": generation}
        raise ValueError("unsupported deployment control operation")


def _try_engine(value: object) -> tuple[Any, ...]:
    """Find the explicitly supported SQLAlchemy engines on a wired store."""
    from sqlalchemy.engine import Engine

    found: list[Engine] = []
    for name in ("_engine", "_conv_engine", "engine"):
        engine = getattr(value, name, None)
        if isinstance(engine, Engine) and engine not in found:
            found.append(engine)
    return tuple(found)


def persistent_state_source(root: Path, stores: tuple[object, ...]) -> PersistentStateGeneration:
    """Bind all wired SQL stores to one disposable persistent-state root."""
    engines: list[Any] = []
    for store in stores:
        engines.extend(_try_engine(store))
    return PersistentStateGeneration(root, tuple(engines))


def bind_sqlalchemy_write_admission(
    coordinator: DeploymentQuiescence, stores: tuple[object, ...]
) -> Callable[[], None]:
    """Count every SQLAlchemy transaction that executes a mutating statement.

    The HTTP and WebSocket fences cover ingress, while this database boundary
    catches persistent writes from callbacks and internal routes as well. A
    write lease stays attached to the checked-out connection until it is
    closed, so a transaction admitted before the fence cannot commit later
    underneath a zero-work observation.
    """
    from sqlalchemy import event

    engines: list[Any] = []
    for store in stores:
        for engine in _try_engine(store):
            if engine not in engines:
                engines.append(engine)

    binding_key = f"omnigent_deployment_write_leases:{coordinator.process_generation}"
    listeners: list[tuple[Any, str, Callable[..., Any]]] = []

    def is_mutating_statement(statement: str, context: Any) -> bool:
        if context is not None and any(
            bool(getattr(context, field, False))
            for field in ("isinsert", "isupdate", "isdelete", "isddl")
        ):
            return True
        stripped = re.sub(r"\A(?:\s|--[^\n]*(?:\n|\Z)|/\*.*?\*/)*", "", statement)
        match = re.match(r"([A-Za-z]+)", stripped)
        if match is None:
            return True
        first = match.group(1).upper()
        if first in {"SELECT", "EXPLAIN"}:
            return False
        if first == "PRAGMA":
            pragma = re.match(r"PRAGMA\s+([A-Za-z_]+)", stripped, re.IGNORECASE)
            if pragma is not None and pragma.group(1).lower() in {
                "database_list",
                "data_version",
                "foreign_key_check",
                "index_info",
                "index_list",
                "table_info",
                "table_xinfo",
                "integrity_check",
                "compile_options",
                "query_only",
            }:
                return False
        # WITH, transaction control, unknown dialect statements and DDL are
        # conservatively admitted as writes. A harmless false positive can
        # temporarily block a certificate; a false negative could lose state.
        return True

    def before_cursor_execute(
        connection: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        context: Any,
        _executemany: bool,
    ) -> None:
        if not is_mutating_statement(statement, context):
            return
        leases_by_transaction = connection.info.setdefault(binding_key, {})
        transaction = connection.get_transaction()
        # Keep each transaction object alive as a dict key. A later transaction
        # on the same pooled connection must take a fresh admission decision,
        # even if an earlier committed transaction's lease is conservatively
        # retained until Connection.close().
        transaction_key = transaction if transaction is not None else object()
        if transaction_key in leases_by_transaction:
            return
        parent = current_admission_lease()
        lease = (
            parent.fork("database_write_transaction")
            if parent is not None
            else coordinator.try_admit("database_write_transaction")
        )
        if lease is None:
            raise QuiescenceBlocked(("database_write_fenced",))
        leases_by_transaction[transaction_key] = lease

    def release_pool_leases(_dbapi_connection: Any, connection_record: Any, *_args: Any) -> None:
        for lease in connection_record.info.pop(binding_key, {}).values():
            lease.release()

    for engine in engines:
        event.listen(engine, "before_cursor_execute", before_cursor_execute)
        listeners.append((engine, "before_cursor_execute", before_cursor_execute))
        for name in ("checkin", "invalidate"):
            event.listen(engine.pool, name, release_pool_leases)
            listeners.append((engine.pool, name, release_pool_leases))

    def unbind() -> None:
        """Remove only these listeners when this application stops."""
        for engine, name, listener in reversed(listeners):
            event.remove(engine, name, listener)

    return unbind


def _provider(name: str, generation: str, active_work: int) -> ComponentObservation:
    if type(active_work) is not int or active_work < 0:
        return ComponentObservation(name, generation, None, "unknown")
    return ComponentObservation(name, generation, active_work)


__all__ = [
    "AdmissionLease",
    "ComponentObservation",
    "DeploymentControlSocket",
    "DeploymentFenceMiddleware",
    "DeploymentQuiescence",
    "PersistentStateGeneration",
    "PersistentStateSnapshot",
    "QuiescenceBlocked",
    "_provider",
    "bind_sqlalchemy_write_admission",
    "current_admission_lease",
    "persistent_state_source",
    "persistent_tree_digest",
]
