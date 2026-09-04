"""Atomic, versioned O3 proposal/audit persistence beneath OMNIGENT_DATA_DIR."""

from __future__ import annotations

import json
import os
import threading
import uuid
from pathlib import Path

from omnigent.process_logging import data_dir

from .models import RoutingProposal

STATE_VERSION = 1
STATE_DIRECTORY_NAME = "o3-routing-review"


class ProposalStore:
    """Small JSON store whose writes are atomic and permission-restricted."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or data_dir() / STATE_DIRECTORY_NAME / "state.json"
        self._lock = threading.RLock()

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {"version": STATE_VERSION, "proposals": {}}
        raw = json.loads(self.path.read_text())
        if not isinstance(raw, dict) or raw.get("version") != STATE_VERSION:
            raise ValueError(f"unsupported O3 routing-review state in {self.path}")
        if not isinstance(raw.get("proposals"), dict):
            raise ValueError(f"invalid O3 routing-review proposals map in {self.path}")
        return raw

    def _write(self, state: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_name(f".{self.path.name}.{uuid.uuid4().hex}.tmp")
        payload = json.dumps(state, indent=2, sort_keys=True) + "\n"
        try:
            fd = os.open(temp, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, self.path)
            os.chmod(self.path, 0o600)
            try:
                parent_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(parent_fd)
                finally:
                    os.close(parent_fd)
            except OSError:
                # Directory fsync is unavailable on some filesystems; the file
                # itself is already flushed and replace remains atomic.
                pass
        finally:
            if temp.exists():
                temp.unlink()

    def put(self, proposal: RoutingProposal) -> None:
        with self._lock:
            state = self._read()
            proposals = state["proposals"]
            assert isinstance(proposals, dict)
            proposals[proposal.proposal_id] = proposal.model_dump(mode="json")
            self._write(state)

    @staticmethod
    def _parse(raw: dict[str, object]) -> RoutingProposal:
        """Read schema-v1 numeric-floor proposals without destructive migration."""
        if raw.get("schema_version", 1) != 1:
            return RoutingProposal.model_validate(raw)
        adapted = json.loads(json.dumps(raw))
        adviser = adapted.get("adviser")
        constraints = adapted.get("approved_constraints")
        if isinstance(adviser, dict):
            legacy = {"low": "easy", "medium": "normal", "high": "hard"}
            original_difficulty = adviser.get("difficulty")
            if isinstance(original_difficulty, str):
                adviser["difficulty"] = legacy.get(original_difficulty, original_difficulty)
            requirements = adviser.get("benchmark_requirements")
            if isinstance(requirements, list):
                for requirement in requirements:
                    if isinstance(requirement, dict):
                        requirement.pop("minimum_score", None)
        if isinstance(constraints, dict):
            constraints.setdefault(
                "difficulty",
                adviser.get("difficulty", "normal") if isinstance(adviser, dict) else "normal",
            )
            constraints.setdefault("calibration_version", "legacy-adviser-numeric-floor")
        adapted["schema_version"] = 2
        return RoutingProposal.model_validate(adapted)

    def get(self, proposal_id: str) -> RoutingProposal | None:
        with self._lock:
            state = self._read()
            proposals = state["proposals"]
            assert isinstance(proposals, dict)
            raw = proposals.get(proposal_id)
            return self._parse(raw) if isinstance(raw, dict) else None

    def list(self) -> list[RoutingProposal]:
        with self._lock:
            state = self._read()
            proposals = state["proposals"]
            assert isinstance(proposals, dict)
            return [self._parse(raw) for raw in proposals.values() if isinstance(raw, dict)]
