"""Atomic, versioned O3 proposal/audit persistence beneath OMNIGENT_DATA_DIR."""

from __future__ import annotations

import hashlib
import json
import os
import threading
import uuid
from pathlib import Path
from typing import Any

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
            from .audit import redact

            payload = redact(proposal.model_dump(mode="json", exclude={"effective_requirements"}))
            extensions: list[dict[str, object]] = []

            def extract(value: object, path: list[str | int]) -> None:
                if isinstance(value, dict):
                    for key in list(value):
                        is_extension = (
                            (not path and key == "audit")
                            or (
                                len(path) == 2
                                and path[0] == "adviser_exchanges"
                                and key
                                in {
                                    "transmitted_model",
                                    "transmitted_effort",
                                    "observed_effort",
                                    "harness",
                                    "duration_ms",
                                    "response",
                                    "response_text",
                                    "response_headers",
                                    "parse_error",
                                    "parsed",
                                }
                            )
                            or (key == "metadata" and "execution_set" in path)
                            or (key == "transport" and "execution_provenance" in path)
                        )
                        if is_extension:
                            extensions.append({"path": [*path, key], "value": value.pop(key)})
                        else:
                            extract(value[key], [*path, key])
                elif isinstance(value, list):
                    for index, item in enumerate(value):
                        extract(item, [*path, index])

            extract(payload, [])
            proposals[proposal.proposal_id] = payload
            audits = state.setdefault("proposal_extensions", {})
            assert isinstance(audits, dict)
            audits[proposal.proposal_id] = {
                "base_hash": hashlib.sha256(
                    json.dumps(payload, sort_keys=True).encode()
                ).hexdigest(),
                "fields": extensions,
            }
            self._write(state)

    @staticmethod
    def _restore(raw: dict[str, object], state: dict[str, object]) -> dict[str, object]:
        import copy

        restored = copy.deepcopy(raw)
        audits = state.get("proposal_extensions", {})
        extension_record = (
            audits.get(raw.get("proposal_id"), {}) if isinstance(audits, dict) else {}
        )
        expected = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        if extension_record.get("base_hash") != expected:
            return restored
        extensions = extension_record.get("fields", [])
        for entry in extensions:
            path = entry["path"]
            target: Any = restored
            try:
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = entry["value"]
            except (KeyError, IndexError, TypeError):
                # An older release may update the legacy portion after rollback.
                continue
        return restored

    def put_failed_review(self, review_id: str, record: dict[str, object]) -> None:
        from .audit import redact

        with self._lock:
            state = self._read()
            failed = state.setdefault("failed_reviews", {})
            assert isinstance(failed, dict)
            failed[review_id] = redact(record)
            self._write(state)

    def get_failed_review(self, review_id: str) -> object:
        with self._lock:
            failed = self._read().get("failed_reviews", {})
            return failed.get(review_id) if isinstance(failed, dict) else None

    @staticmethod
    def _parse(raw: dict[str, object]) -> RoutingProposal:
        """Read schema-v1 numeric-floor proposals without destructive migration."""
        raw = {key: value for key, value in raw.items() if key != "effective_requirements"}
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
            return self._parse(self._restore(raw, state)) if isinstance(raw, dict) else None

    def list(self) -> list[RoutingProposal]:
        with self._lock:
            state = self._read()
            proposals = state["proposals"]
            assert isinstance(proposals, dict)
            return [
                self._parse(self._restore(raw, state))
                for raw in proposals.values()
                if isinstance(raw, dict)
            ]
