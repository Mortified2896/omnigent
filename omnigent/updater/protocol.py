"""Durable request and result schemas (issue #38 §1, §8).

The request interface only accepts the narrow schema documented
below. Arbitrary shell commands, service names, paths, remotes,
scripts, and environment overrides are **never** accepted — the
request layer is a pure data carrier so a malicious or buggy caller
cannot smuggle deployment privileges into the updater.

Result records are produced by the controller when a request reaches
a terminal state; the controller persists them to disk **before**
attempting delivery so a crash between persistence and delivery is
recoverable on restart.
"""

from __future__ import annotations

import re
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from omnigent.updater.state_machine import UpdatePhase

_REQUEST_ID_PATTERN = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")

TERMINAL_STATES: frozenset[str] = frozenset(
    {
        UpdatePhase.SUCCEEDED.value,
        UpdatePhase.REJECTED.value,
        UpdatePhase.FAILED.value,
        UpdatePhase.ROLLED_BACK.value,
        UpdatePhase.ROLLBACK_FAILED.value,
    }
)


def _now_iso() -> str:
    """UTC timestamp with second precision — RFC3339-ish, no microseconds.

    Microsecond precision is overkill for human-readable audit logs
    and complicates deterministic test assertions.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def new_request_id() -> str:
    """Generate a fresh 26-character Crockford-base32 ULID-ish id.

    The strict format check is what makes the id safe to embed in
    file paths, shell snippets, and HTTP routes without escaping.
    The implementation is a random 16-byte UUID written in the
    Crockford base32 alphabet — not a true monotonic ULID — but the
    length and charset match, which is what the rest of the
    protocol cares about.
    """
    raw = uuid.uuid4().bytes
    alphabet = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
    out = []
    value = int.from_bytes(raw, "big")
    for _ in range(26):
        value, rem = divmod(value, 32)
        out.append(alphabet[rem])
    return "".join(reversed(out))


def is_valid_request_id(value: Any) -> bool:
    """Return whether ``value`` is a syntactically valid request id."""
    return isinstance(value, str) and bool(_REQUEST_ID_PATTERN.match(value))


def is_valid_sha(value: Any) -> bool:
    """Return whether ``value`` is a syntactically valid 40-char lowercase hex SHA."""
    return isinstance(value, str) and bool(_SHA_PATTERN.match(value))


@dataclass(frozen=True)
class Authorization:
    """Authorization metadata recorded with every update request.

    The request layer never *evaluates* authorization — that is the
    job of the operator's pre-flight check or the systemd unit's
    caller identity. This object just records who claimed to file
    the request so audit logs are honest.

    :param kind: One of ``"operator"``, ``"verity"``, ``"system"``.
    :param operator: Free-form operator name, e.g. ``"hermes"``. Required
        when ``kind == "operator"``.
    :param source_ip: Optional source IP for an operator invocation.
    :param notes: Free-form audit notes — never interpreted by the
        controller.
    """

    kind: str
    operator: str | None = None
    source_ip: str | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in {"operator", "verity", "system"}:
            raise ValueError(
                f"authorization.kind must be 'operator', 'verity', or 'system'; got {self.kind!r}"
            )
        if self.kind == "operator" and not self.operator:
            raise ValueError("authorization.operator is required for kind='operator'")

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in asdict(self).items() if v is not None}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Authorization:
        return cls(
            kind=str(data["kind"]),
            operator=data.get("operator"),
            source_ip=data.get("source_ip"),
            notes=data.get("notes"),
        )


@dataclass(frozen=True)
class RequestRecord:
    """Durable record of an update request.

    The set of fields is intentionally narrow. ``extra`` is rejected
    by :func:`from_dict` so an over-permissive caller cannot smuggle
    additional deployment privileges into the updater.

    :param request_id: 26-char ULID-shaped identifier.
    :param target_sha: 40-char lowercase hex target SHA.
    :param expected_current_sha: 40-char lowercase hex expected live SHA.
    :param origin_session_id: Conversation id of the originating session,
        if the request came from a conversation. ``None`` for pure
        operator requests.
    :param origin_conversation_id: Conversation id if distinct from
        ``origin_session_id``; otherwise ``None``.
    :param requested_by: Identity string (``"operator:<name>"``,
        ``"verity"``, ``"system:<agent>"``).
    :param created_at: RFC3339 timestamp.
    :param authorization: Authorization metadata (see :class:`Authorization`).
    :param repository: ``owner/repo`` of the approved writable fork the
        target SHA was drawn from. Defaults to the configured fork
        (``Mortified2896/omnigent``).
    :param approved_branch: Branch the target SHA was merged into on
        the approved fork. Defaults to ``"main"``.
    :param github_issue_number: Issue number that produced the merged
        SHA, when applicable. ``None`` if the deployment is not tied
        to an issue.
    :param github_pr_number: PR number that merged the implementation
        into the approved branch. ``None`` if not tied to a PR.
    :param notes: Free-form notes — never interpreted.
    """

    request_id: str
    target_sha: str
    expected_current_sha: str
    origin_session_id: str | None
    origin_conversation_id: str | None
    requested_by: str
    created_at: str
    authorization: Authorization
    repository: str = "Mortified2896/omnigent"
    approved_branch: str = "main"
    github_issue_number: int | None = None
    github_pr_number: int | None = None
    notes: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_request_id(self.request_id):
            raise ValueError(
                f"request_id must be a 26-char ULID-shaped string; got {self.request_id!r}"
            )
        if not is_valid_sha(self.target_sha):
            raise ValueError(
                f"target_sha must be 40 lowercase hex characters; got {self.target_sha!r}"
            )
        if not is_valid_sha(self.expected_current_sha):
            raise ValueError(
                f"expected_current_sha must be 40 lowercase hex characters; "
                f"got {self.expected_current_sha!r}"
            )
        if "/" not in self.repository:
            raise ValueError(f"repository must be 'owner/repo'; got {self.repository!r}")
        if not self.approved_branch:
            raise ValueError("approved_branch must be a non-empty string")
        if self.github_issue_number is not None and self.github_issue_number < 1:
            raise ValueError(
                f"github_issue_number must be a positive integer; got {self.github_issue_number!r}"
            )
        if self.github_pr_number is not None and self.github_pr_number < 1:
            raise ValueError(
                f"github_pr_number must be a positive integer; got {self.github_pr_number!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        out = {
            "request_id": self.request_id,
            "target_sha": self.target_sha,
            "expected_current_sha": self.expected_current_sha,
            "origin_session_id": self.origin_session_id,
            "origin_conversation_id": self.origin_conversation_id,
            "requested_by": self.requested_by,
            "created_at": self.created_at,
            "authorization": self.authorization.to_dict(),
            "repository": self.repository,
            "approved_branch": self.approved_branch,
        }
        if self.github_issue_number is not None:
            out["github_issue_number"] = self.github_issue_number
        if self.github_pr_number is not None:
            out["github_pr_number"] = self.github_pr_number
        if self.notes:
            out["notes"] = self.notes
        return out

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RequestRecord:
        # Strict whitelist: an extra field is silently dropped on
        # read, but writing an extra field is rejected by the
        # controller. This asymmetry is intentional — the on-disk
        # schema can grow forward without invalidating older request
        # files, but a brand-new request cannot claim a privileged
        # field.
        expected = {
            "request_id",
            "target_sha",
            "expected_current_sha",
            "origin_session_id",
            "origin_conversation_id",
            "requested_by",
            "created_at",
            "authorization",
            "notes",
            "repository",
            "approved_branch",
            "github_issue_number",
            "github_pr_number",
        }
        unknown = set(data) - expected
        if unknown:
            raise ValueError(f"request has unknown fields: {sorted(unknown)}")
        kwargs: dict[str, Any] = {
            "request_id": str(data["request_id"]),
            "target_sha": str(data["target_sha"]),
            "expected_current_sha": str(data["expected_current_sha"]),
            "origin_session_id": data.get("origin_session_id"),
            "origin_conversation_id": data.get("origin_conversation_id"),
            "requested_by": str(data["requested_by"]),
            "created_at": str(data["created_at"]),
            "authorization": Authorization.from_dict(data["authorization"]),
            "notes": data.get("notes"),
        }
        if "repository" in data:
            kwargs["repository"] = str(data["repository"])
        if "approved_branch" in data:
            kwargs["approved_branch"] = str(data["approved_branch"])
        if data.get("github_issue_number") is not None:
            kwargs["github_issue_number"] = int(data["github_issue_number"])
        if data.get("github_pr_number") is not None:
            kwargs["github_pr_number"] = int(data["github_pr_number"])
        return cls(**kwargs)


@dataclass
class ResultRecord:
    """Durable record of an update's terminal result.

    Persisted to disk before delivery is attempted. The web service
    reconciles pending deliveries on startup so the operator always
    learns the final outcome.

    :param request_id: The request this result belongs to.
    :param final_status: One of ``"succeeded"``, ``"rejected"``,
        ``"failed"``, ``"rolled_back"``, ``"rollback_failed"``.
    :param target_sha: The target SHA the request named.
    :param previous_sha: The live SHA at validation time.
    :param deployed_sha: The live SHA after the controller finished.
    :param started_at: RFC3339 timestamp when validation finished
        and the controller transitioned out of ``validating``.
    :param finished_at: RFC3339 timestamp of the terminal transition.
    :param failure_phase: Phase that produced the failure, if any.
    :param failure_reason: Short human-readable failure reason.
    :param rollback_performed: True iff rollback was attempted.
    :param rollback_result: ``"succeeded"``, ``"failed"``, or ``None``.
    :param notification_status: ``"pending"``, ``"delivered"``,
        ``"failed"``, or ``"not_applicable"``.
    :param events_tail: A short tail of the last few events for
        forensics. The full event log lives in the append-only JSONL.
    """

    request_id: str
    final_status: str
    target_sha: str
    previous_sha: str
    deployed_sha: str
    started_at: str
    finished_at: str
    failure_phase: str = ""
    failure_reason: str = ""
    rollback_performed: bool = False
    rollback_result: str | None = None
    notification_status: str = "pending"
    events_tail: list[dict[str, Any]] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.final_status not in TERMINAL_STATES:
            raise ValueError(f"final_status must be a terminal state; got {self.final_status!r}")
        if not is_valid_sha(self.target_sha):
            raise ValueError(
                f"target_sha must be 40 lowercase hex characters; got {self.target_sha!r}"
            )
        if self.previous_sha and not is_valid_sha(self.previous_sha):
            raise ValueError(
                f"previous_sha must be 40 lowercase hex characters when set; "
                f"got {self.previous_sha!r}"
            )
        if self.deployed_sha and not is_valid_sha(self.deployed_sha):
            raise ValueError(
                f"deployed_sha must be 40 lowercase hex characters when set; "
                f"got {self.deployed_sha!r}"
            )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ResultRecord:
        return cls(
            request_id=str(data["request_id"]),
            final_status=str(data["final_status"]),
            target_sha=str(data["target_sha"]),
            previous_sha=str(data.get("previous_sha", "")),
            deployed_sha=str(data.get("deployed_sha", "")),
            started_at=str(data.get("started_at", "")),
            finished_at=str(data.get("finished_at", "")),
            failure_phase=str(data.get("failure_phase", "")),
            failure_reason=str(data.get("failure_reason", "")),
            rollback_performed=bool(data.get("rollback_performed", False)),
            rollback_result=data.get("rollback_result"),
            notification_status=str(data.get("notification_status", "pending")),
            events_tail=list(data.get("events_tail", [])),
        )

    @classmethod
    def rejection(
        cls,
        *,
        request_id: str,
        target_sha: str,
        finished_at: str | None = None,
        reason: str = "",
    ) -> ResultRecord:
        """Construct a ``rejected`` result for a request that failed validation."""
        return cls(
            request_id=request_id,
            final_status=UpdatePhase.REJECTED.value,
            target_sha=target_sha,
            previous_sha="",
            deployed_sha="",
            started_at="",
            finished_at=finished_at or _now_iso(),
            failure_phase=UpdatePhase.VALIDATING.value,
            failure_reason=reason,
            notification_status="pending",
        )


def now_iso() -> str:
    """Public alias for the internal timestamp helper."""
    return _now_iso()
