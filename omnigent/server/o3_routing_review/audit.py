"""Safe capture and immutable decision revisions in the existing proposal store."""

from __future__ import annotations

import copy
import os
import re
from typing import Any

from .models import RoutingProposal

_SECRET_KEY = re.compile(
    r"(?:authorization|cookie|password|passwd|credential|secret|api[_-]?key|access[_-]?token|refresh[_-]?token|bearer[_-]?token|private[_-]?key)",
    re.I,
)
_BEARER = re.compile(r"(?i)\bBearer\s+[^\s\"'<>]+")
_ASSIGNMENT = re.compile(
    r"(?i)((?:[\w-]*(?:api[_-]?key|password|secret|credential|access[_-]?token|refresh[_-]?token)[\w-]*)[\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
)
_KEY_VALUE = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{12,}|gh[pousr]_[A-Za-z0-9_]{16,})\b")


def redact(value: Any, *, secrets: tuple[str, ...] = ()) -> Any:
    """Preserve safe values and whitespace; remove credentials before capture."""
    known = tuple(v for k, v in os.environ.items() if _SECRET_KEY.search(k) and len(v) >= 4)

    def walk(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                k: "[REDACTED]" if _SECRET_KEY.search(str(k)) else walk(v) for k, v in item.items()
            }
        if isinstance(item, list):
            return [walk(v) for v in item]
        if isinstance(item, str):
            for secret in sorted((*known, *secrets), key=len, reverse=True):
                if secret:
                    item = item.replace(secret, "[REDACTED]")
            item = _BEARER.sub("Bearer [REDACTED]", item)
            item = _ASSIGNMENT.sub(r"\1[REDACTED]", item)
            return _KEY_VALUE.sub("[REDACTED]", item)
        return item

    return walk(value)


def snapshot(proposal: RoutingProposal, event: str) -> RoutingProposal:
    """Retain actual inputs/results of each changed decision, without rewriting history."""
    if proposal.audit is None:
        return proposal
    audit = copy.deepcopy(proposal.audit)
    history = audit.setdefault("history", [])
    assert isinstance(history, list)
    state = redact(
        proposal.model_dump(
            mode="json",
            include={
                "approved_constraints",
                "requirement_overrides",
                "effective_requirements",
                "recommendation",
                "evaluations",
                "decision",
                "decision_reason",
                "derived_combo_name",
                "derived_combo_definition",
                "selected_execution",
                "execution_options",
                "execution_exclusions",
                "execution_provenance",
                "actual_model",
                "actual_provider",
                "actual_reasoning_effort",
                "resource_snapshot",
                "resource_advice",
                "tool_free_provenance",
                "execution_status",
            },
        )
    )
    if not history or history[-1].get("state") != state:
        history.append({"event": event, "at": proposal.updated_at.isoformat(), "state": state})
    proposal.audit = audit
    return proposal
