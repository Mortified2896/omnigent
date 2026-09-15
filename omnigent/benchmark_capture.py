"""Capture reproducible Git workspace state for future benchmark construction."""

from __future__ import annotations

import hashlib
import json
import subprocess
import tarfile
from pathlib import Path
from typing import Any


class BenchmarkCaptureError(RuntimeError):
    """Raised when a benchmark workspace snapshot cannot be captured safely."""


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            ["git", "--no-optional-locks", "-C", str(repo), *args],
            check=check,
            capture_output=True,
            timeout=30,
        )
    except FileNotFoundError as exc:
        raise BenchmarkCaptureError("git is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()
        raise BenchmarkCaptureError(
            f"git {' '.join(args)} failed{f': {detail}' if detail else ''}"
        ) from exc


def _git_text(repo: Path, *args: str, check: bool = True) -> str | None:
    result = _git(repo, *args, check=check)
    if not check and result.returncode != 0:
        return None
    value = result.stdout.decode("utf-8", errors="strict").strip()
    return value or None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _artifact(path: Path) -> dict[str, Any]:
    return {
        "path": path.name,
        "size": path.stat().st_size,
        "sha256": _sha256(path),
    }


def _safe_untracked_path(raw: str) -> Path:
    rel = Path(raw)
    if rel.is_absolute() or ".." in rel.parts or rel == Path("."):
        raise BenchmarkCaptureError(f"unsafe untracked path returned by git: {raw!r}")
    return rel


def snapshot_git_state(repo: Path, destination: Path) -> dict[str, Any]:
    """Capture a read-only, reconstructable snapshot of one Git worktree.

    The repository itself is never mutated. ``destination`` must live outside
    the repository so the capture cannot become part of the state it records.
    """

    requested_repo = repo.resolve()
    root_raw = _git_text(requested_repo, "rev-parse", "--show-toplevel")
    if root_raw is None:
        raise BenchmarkCaptureError(f"{requested_repo} is not a Git worktree")
    root = Path(root_raw).resolve()

    dest = destination.resolve(strict=False)
    if dest == root or dest.is_relative_to(root):
        raise BenchmarkCaptureError("benchmark capture destination must be outside the repository")

    head = _git_text(root, "rev-parse", "HEAD")
    if head is None:
        raise BenchmarkCaptureError("repository has no resolvable HEAD")

    branch = _git_text(root, "symbolic-ref", "--quiet", "--short", "HEAD", check=False)
    origin = _git_text(root, "config", "--get", "remote.origin.url", check=False)

    status = _git(root, "status", "--porcelain=v1", "-z", "--untracked-files=all").stdout
    staged = _git(
        root,
        "diff",
        "--cached",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ).stdout
    unstaged = _git(
        root,
        "diff",
        "--binary",
        "--full-index",
        "--no-ext-diff",
        "--no-textconv",
        "--no-color",
        "--src-prefix=a/",
        "--dst-prefix=b/",
    ).stdout
    untracked_raw = _git(root, "ls-files", "--others", "--exclude-standard", "-z").stdout
    untracked = [
        _safe_untracked_path(item.decode("utf-8", errors="surrogateescape"))
        for item in untracked_raw.split(b"\0")
        if item
    ]

    dest.mkdir(parents=True, exist_ok=False)

    status_path = dest / "status.z"
    staged_path = dest / "staged.patch"
    unstaged_path = dest / "unstaged.patch"
    untracked_path = dest / "untracked.tar"

    status_path.write_bytes(status)
    staged_path.write_bytes(staged)
    unstaged_path.write_bytes(unstaged)

    try:
        with tarfile.open(untracked_path, mode="w", dereference=False) as archive:
            for rel in untracked:
                source = root / rel
                if not source.exists() and not source.is_symlink():
                    raise BenchmarkCaptureError(
                        f"untracked path disappeared during capture: {rel.as_posix()}"
                    )
                archive.add(source, arcname=rel.as_posix(), recursive=False)
    except Exception:
        # A partial archive must never masquerade as a complete snapshot.
        untracked_path.unlink(missing_ok=True)
        raise

    metadata: dict[str, Any] = {
        "schema_version": 1,
        "repo_root": str(root),
        "origin": origin,
        "head": head,
        "branch": branch,
        "detached": branch is None,
        "untracked_paths": [path.as_posix() for path in untracked],
        "artifacts": {
            "status": _artifact(status_path),
            "staged_patch": _artifact(staged_path),
            "unstaged_patch": _artifact(unstaged_path),
            "untracked_archive": _artifact(untracked_path),
        },
    }
    (dest / "git.json").write_text(
        json.dumps(metadata, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return metadata


def capture_enabled() -> bool:
    import os

    return os.environ.get("OMNIGENT_BENCHMARK_CAPTURE") == "1"


class TurnCapture:
    """Best-effort provenance for one dispatch; native trajectories stay native."""

    def __init__(self, directory: Path, manifest: dict[str, Any]) -> None:
        self.directory = directory
        self.manifest = manifest
        self.native_home: Path | None = None
        self.native_path: str | None = None
        self.finished = False

    @classmethod
    def begin(
        cls,
        *,
        cwd: str,
        session_id: str | None,
        turn_id: str | None,
        harness: str,
        task_input: dict[str, Any],
    ) -> TurnCapture | None:
        if not capture_enabled():
            return None
        import uuid

        from omnigent.version import VERSION

        try:
            root = capture_root()
            repo_root = Path(_git_text(Path(cwd), "rev-parse", "--show-toplevel") or cwd).resolve()
            if root.is_relative_to(repo_root):
                raise BenchmarkCaptureError("capture root must be outside the repository")
            capture_id = str(uuid.uuid4())
            directory = root / capture_id
            directory.mkdir(parents=True, mode=0o700)
            capture = cls(
                directory,
                {
                    "schema_version": 1,
                    "capture_id": capture_id,
                    "omnigent_session_id": session_id,
                    "omnigent_turn_id": turn_id,
                    "task_identity": turn_id or capture_id,
                    "harness": harness,
                    "repo_root": str(repo_root),
                    "started_at": _now(),
                    "completed_at": None,
                    "terminal_state": "running",
                    "codex_thread_id": None,
                    "codex_turn_id": None,
                    "native_rollout": None,
                    "start": None,
                    "end": None,
                    "runtime": {
                        "omnigent_version": VERSION,
                        "omnigent_source_commit": _source_commit(),
                    },
                    "requested_model": task_input.get("model"),
                    "observed_model": None,
                    "requested_reasoning_effort": task_input.get("reasoning_effort"),
                    "observed_reasoning_effort": None,
                    "routing": dict.fromkeys(
                        (
                            "policy",
                            "provider",
                            "connection_id",
                            "canonical_model",
                            "combo",
                            "strategy",
                            "request_id",
                            "correlation_id",
                            "retries",
                            "fallback_used",
                            "o3_proposal_id",
                        )
                    ),
                    "outcome": None,
                    "artifacts": {},
                    "errors": [],
                },
            )
            capture.attempt("input", lambda: capture._write_artifact("input.json", task_input))
            capture.attempt("start", lambda: capture._snapshot("start"))
            capture.persist()
            capture.attempt("otel", capture._annotate_span)
            return capture
        except Exception:  # noqa: BLE001 - optional recorder must never fail execution
            # No paths, prompts, or subprocess stderr in operational logs.
            import logging

            logging.getLogger(__name__).warning("Benchmark capture initialization failed")
            return None

    def _annotate_span(self) -> None:
        try:
            from opentelemetry import trace
        except ImportError:
            return

        span = trace.get_current_span()
        if span.is_recording():
            span.set_attribute("omnigent.capture_id", self.manifest["capture_id"])

    def attempt(self, stage: str, operation: Any) -> None:
        try:
            operation()
        except Exception as exc:  # noqa: BLE001 - retain partial evidence without failing execution
            self.manifest["errors"].append({"stage": stage, "type": type(exc).__name__})

    def _snapshot(self, stage: str) -> None:
        self.manifest[stage] = snapshot_git_state(
            Path(self.manifest["repo_root"]), self.directory / stage
        )

    def persist(self) -> None:
        self.attempt(
            "manifest", lambda: _write_json(self.directory / "manifest.json", self.manifest)
        )

    def bind(
        self,
        *,
        thread_id: str | None = None,
        turn_id: str | None = None,
        native_home: Path | None = None,
        native_path: str | None = None,
    ) -> None:
        if thread_id:
            self.manifest["codex_thread_id"] = thread_id
        if turn_id:
            self.manifest["codex_turn_id"] = turn_id
        if native_home:
            self.native_home = native_home
        if native_path:
            self.native_path = native_path
        self.persist()

    def _write_artifact(self, name: str, value: Any) -> None:
        path = self.directory / name
        _write_json(path, value)
        self.manifest["artifacts"][name] = _artifact(path)

    def record_input(self, params: dict[str, Any]) -> None:
        self.attempt("effective_input", lambda: self._write_artifact("codex-input.json", params))

    def finish(self, state: str, outcome: dict[str, Any] | None = None) -> None:
        if self.finished:
            return
        self.finished = True
        self.manifest.update(terminal_state=state, completed_at=_now(), outcome=outcome)
        from datetime import datetime

        self.manifest["duration_seconds"] = (
            datetime.fromisoformat(self.manifest["completed_at"])
            - datetime.fromisoformat(self.manifest["started_at"])
        ).total_seconds()
        self.attempt("end", lambda: self._snapshot("end"))
        if self.manifest["harness"] == "codex":
            self.attempt("trajectory", self._preserve_native)
        self.persist()

    def _preserve_native(self) -> None:
        from omnigent.benchmark_capture_codex import preserve_rollout

        evidence = preserve_rollout(
            home=self.native_home,
            explicit_path=self.native_path,
            thread_id=self.manifest["codex_thread_id"],
            turn_id=self.manifest["codex_turn_id"],
            destination=self.directory / "trajectory",
        )
        self.manifest["native_rollout"] = evidence
        self.manifest["observed_model"] = evidence.get("model")
        self.manifest["observed_model_source"] = (
            "codex.turn_context" if evidence.get("model") else None
        )
        self.manifest["observed_reasoning_effort"] = evidence.get("reasoning_effort")
        self.manifest["runtime"]["codex_version"] = evidence.get("codex_version")


def _source_commit() -> str | None:
    # Only identify a source checkout; a wheel's parent may be an unrelated repo.
    root = Path(__file__).resolve().parent.parent
    return _git_text(root, "rev-parse", "HEAD", check=False) if (root / ".git").exists() else None


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


def _write_json(path: Path, value: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


async def capture_io(operation: Any, *args: Any, **kwargs: Any) -> Any:
    """Drain capture I/O on cancellation and isolate all recorder exceptions."""
    import asyncio

    async def run() -> Any:
        try:
            return await asyncio.to_thread(operation, *args, **kwargs)
        except Exception:  # noqa: BLE001 - optional recorder must never fail execution
            return None

    task = asyncio.create_task(run())
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        result = await task
        if isinstance(result, TurnCapture):
            await asyncio.to_thread(result.finish, "cancelled")
        raise


def capture_root() -> Path:
    import os

    from omnigent.process_logging import data_dir

    return (
        Path(os.environ.get("OMNIGENT_BENCHMARK_CAPTURE_DIR") or data_dir() / "benchmark-captures")
        .expanduser()
        .resolve()
    )
