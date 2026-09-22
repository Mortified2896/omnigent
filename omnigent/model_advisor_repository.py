"""Durable advisor records on a caller-supplied SQLAlchemy engine.

No private SQLite files, constructor-time DDL, provider calls or global runtime
registration. Application integration must supply its existing database engine,
install this table through a reviewed migration, and authorize owner/host access.
Only reserve_round(acquired=True) may call the advisor; only
confirm(acquired=True) may start an executor. finish_advice never dispatches.
A crash after a claim is intentionally NOT retried blindly (at-most-once claim,
not a guarantee of exactly-once external provider execution).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import (
    Column,
    Engine,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    insert,
    select,
    update,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError

from omnigent.model_advisor_core import AdvisorContractError, PoolSnapshot
from omnigent.model_advisor_workflow import (
    AdvisorPreferences,
    FrozenRound,
    ReviewDecision,
    canonical_json,
    confirm_review,
    document_digest,
    prepare_review,
)

metadata = MetaData()
records = Table(
    "model_advisor_records",
    metadata,
    Column("scope_key", String(64), primary_key=True),
    Column("version", Integer, nullable=False),
    Column("state", String(32), nullable=False),
    Column("payload", Text().with_variant(LONGTEXT(), "mysql"), nullable=False),
)


class AdvisorConflict(AdvisorContractError):
    """Changed settings/round or an already claimed operation (HTTP 409/412)."""


@dataclass(frozen=True)
class Record:
    version: int
    state: str
    payload: dict

    @property
    def etag(self) -> str:
        return f'"advisor-{self.version}-{document_digest(self.payload)}"'


@dataclass(frozen=True)
class Claim:
    record: Record
    acquired: bool


def _key(owner: str, host: str, kind: str, identity: str) -> str:
    for value in (owner, host, identity):
        if (
            not isinstance(value, str)
            or not value.strip()
            or value != value.strip()
            or len(value) > 256
        ):
            raise AdvisorContractError("Invalid record scope")
    return document_digest(("advisor-v1", owner, host, kind, identity))


def _expected(version: int) -> None:
    if type(version) is not int or version < 0:
        raise AdvisorConflict("An explicit nonnegative version is required")


class AdvisorRepository:
    """Source-ready persistence primitive; no API route is registered here."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def _read(self, connection: Connection, key: str) -> Record | None:
        import json

        row = (
            connection.execute(select(records).where(records.c.scope_key == key))
            .mappings()
            .first()
        )
        return (
            None
            if row is None
            else Record(row["version"], row["state"], json.loads(row["payload"]))
        )

    def _required(self, connection: Connection, key: str) -> Record:
        result = self._read(connection, key)
        if result is None:
            raise AdvisorConflict("Record not found in this owner/host scope")
        return result

    def _cas(
        self, connection: Connection, key: str, previous: Record, state: str, payload: dict
    ) -> Record:
        result = connection.execute(
            update(records)
            .where(
                records.c.scope_key == key,
                records.c.version == previous.version,
                records.c.state == previous.state,
            )
            .values(version=previous.version + 1, state=state, payload=canonical_json(payload))
        )
        if result.rowcount != 1:
            raise AdvisorConflict(
                "Concurrent update; reload the existing record, do not retry execution"
            )
        return Record(previous.version + 1, state, payload)

    def load_preferences(self, owner: str, host: str, profile: str = "default") -> Record | None:
        with self.engine.connect() as connection:
            return self._read(connection, _key(owner, host, "preferences", profile))

    def save_preferences(
        self,
        owner: str,
        host: str,
        preferences: AdvisorPreferences,
        *,
        expected_version: int,
        profile: str = "default",
    ) -> Record:
        """Version 0 means create only; a stale save cannot overwrite another tab."""
        _expected(expected_version)
        key = _key(owner, host, "preferences", profile)
        payload = preferences.to_payload()
        try:
            with self.engine.begin() as connection:
                previous = self._read(connection, key)
                if previous is None:
                    if expected_version != 0:
                        raise AdvisorConflict("Preferences were not created at that version")
                    connection.execute(
                        insert(records).values(
                            scope_key=key,
                            version=1,
                            state="saved",
                            payload=canonical_json(payload),
                        )
                    )
                    return Record(1, "saved", payload)
                if previous.version != expected_version:
                    raise AdvisorConflict("Preferences changed in another tab")
                return self._cas(connection, key, previous, "saved", payload)
        except IntegrityError as exc:
            raise AdvisorConflict("Preferences created concurrently; reload first") from exc

    def reserve_round(self, frozen: FrozenRound, *, submission_key: str | None = None) -> Claim:
        """Atomic create-before-advisor-call. Duplicate submissions receive no claim."""
        key = _key(frozen.owner_id, frozen.host_id, "round", frozen.round_id)
        payload = {
            "fingerprint": frozen.fingerprint,
            "frozen": frozen.to_payload(),
            # This is audit metadata only. The durable key remains scoped by
            # owner + host + round id, and the frozen fingerprint is what
            # prevents a same-id request from changing the reservation.
            "submission_key": submission_key,
        }
        try:
            with self.engine.begin() as connection:
                connection.execute(
                    insert(records).values(
                        scope_key=key,
                        version=1,
                        state="advisor_pending",
                        payload=canonical_json(payload),
                    )
                )
            return Claim(Record(1, "advisor_pending", payload), True)
        except IntegrityError as exc:
            with self.engine.connect() as connection:
                previous = self._required(connection, key)
            if previous.payload["fingerprint"] != frozen.fingerprint:
                raise AdvisorConflict(
                    "Round ID reused with a different prompt, pool or settings"
                ) from exc
            return Claim(previous, False)

    def load_round(self, owner: str, host: str, round_id: str) -> Record | None:
        with self.engine.connect() as connection:
            return self._read(connection, _key(owner, host, "round", round_id))

    def finish_advice(
        self,
        owner: str,
        host: str,
        round_id: str,
        raw_advice: str,
        *,
        randbelow: Callable[[int], int],
    ) -> Claim:
        """Store selection/draw once; only a claimed transaction can draw.

        This is an internal callback, not a browser-selected recommendation.
        No provider I/O occurs under the transaction. Invalid advice rolls it
        back; the orchestration layer must mark the failed round blocked.
        """
        key = _key(owner, host, "round", round_id)
        advice_digest = document_digest(raw_advice)
        with self.engine.begin() as connection:
            previous = self._required(connection, key)
            if "review" in previous.payload:
                if previous.payload["advice_digest"] != advice_digest:
                    raise AdvisorConflict("Advisor result changed after it was committed")
                return Claim(previous, False)
            if previous.state != "advisor_pending":
                raise AdvisorConflict("Round no longer accepts advice")
            # UPDATE acquires a database write lock before sampling. Failure
            # cannot publish the intermediate state or an uncommitted draw.
            locked = self._cas(connection, key, previous, "assigning", previous.payload)
            frozen = FrozenRound.from_payload(previous.payload["frozen"])
            review = prepare_review(frozen, raw_advice, randbelow=randbelow)
            payload = {
                **previous.payload,
                "advice_digest": advice_digest,
                "review": review.to_payload(),
            }
            result = self._cas(connection, key, locked, "awaiting_confirmation", payload)
            return Claim(result, True)

    def confirm(
        self,
        owner: str,
        host: str,
        round_id: str,
        *,
        expected_version: int,
        live_catalog: PoolSnapshot,
        override_candidate_id: str | None = None,
        reason: str | None = None,
    ) -> Claim:
        """Commit an at-most-once execution claim after explicit user confirmation.

        The caller must independently recheck exact model/effort/lane at dispatch.
        In particular it must NOT use a generic model-fallback/reset path.
        """
        _expected(expected_version)
        key = _key(owner, host, "round", round_id)
        confirmation = document_digest((override_candidate_id, reason))
        with self.engine.begin() as connection:
            previous = self._required(connection, key)
            if "confirmation" in previous.payload:
                if previous.payload["confirmation"] != confirmation:
                    raise AdvisorConflict("The round was already confirmed differently")
                return Claim(previous, False)
            if previous.version != expected_version or previous.state != "awaiting_confirmation":
                raise AdvisorConflict("Review changed or is not ready to execute")
            frozen = FrozenRound.from_payload(previous.payload["frozen"])
            review = ReviewDecision.from_payload(previous.payload["review"])
            final = confirm_review(
                frozen,
                review,
                live_catalog,
                override_candidate_id=override_candidate_id,
                reason=reason,
            )
            payload = {
                **previous.payload,
                "review": final.to_payload(),
                "confirmation": confirmation,
                "dispatch_id": "advisor-exec-" + frozen.fingerprint,
                "actual_execution": None,
            }
            return Claim(self._cas(connection, key, previous, "dispatch_claimed", payload), True)

    def cancel(self, owner: str, host: str, round_id: str, *, expected_version: int) -> Record:
        """Cancel pre-execution only; this does not stop an already-running process."""
        _expected(expected_version)
        key = _key(owner, host, "round", round_id)
        with self.engine.begin() as connection:
            previous = self._required(connection, key)
            if previous.version != expected_version or previous.state not in {
                "advisor_pending",
                "awaiting_confirmation",
            }:
                raise AdvisorConflict("Round changed or execution was already claimed")
            return self._cas(connection, key, previous, "cancelled", previous.payload)

    def mark_round_failed(self, owner: str, host: str, round_id: str, *, reason: str) -> Record:
        """Record a failed or timed-out advisor step as visibly blocked.

        Terminal and never retried automatically: the round's single advisor
        permit was already consumed, so a blocked round can only be abandoned
        in favor of an explicit new round. This is also the honest record for
        an interrupted advisor call whose in-flight completion is unknown.
        """
        if not isinstance(reason, str) or not reason.strip() or len(reason) > 600:
            raise AdvisorContractError("A short failure reason is required")
        key = _key(owner, host, "round", round_id)
        with self.engine.begin() as connection:
            previous = self._required(connection, key)
            if previous.state != "advisor_pending":
                raise AdvisorConflict("Only a pending advisor round can be marked failed")
            payload = {**previous.payload, "failure_reason": reason.strip()}
            return self._cas(connection, key, previous, "blocked", payload)

    def bind_dispatch(self, owner: str, host: str, round_id: str, *, session_id: str) -> Claim:
        """Durably bind the confirmed round to the exact created session.

        Called by the orchestration layer after the at-most-once claim
        created its one session. A crash between ``confirm`` and this call
        leaves the record in ``dispatch_claimed`` without a binding: an
        explicitly uncertain dispatch that must be inspected, never replayed.
        Rebinding the same session is idempotent; a different one conflicts.
        """
        if not isinstance(session_id, str) or not session_id.strip() or len(session_id) > 256:
            raise AdvisorContractError("Invalid session binding")
        key = _key(owner, host, "round", round_id)
        with self.engine.begin() as connection:
            previous = self._required(connection, key)
            bound = previous.payload.get("execution_session_id")
            if bound is not None:
                if bound != session_id:
                    raise AdvisorConflict("Round is already bound to another session")
                return Claim(previous, False)
            if previous.state != "dispatch_claimed":
                raise AdvisorConflict("Only a claimed round can bind its execution session")
            payload = {**previous.payload, "execution_session_id": session_id}
            return Claim(self._cas(connection, key, previous, "dispatch_bound", payload), True)

    def record_advisor_overhead(
        self, owner: str, host: str, round_id: str, *, overhead: dict
    ) -> Record | None:
        """Best-effort telemetry merge (usage/latency); never changes state.

        Lost races with confirmation or cancellation drop the telemetry —
        it is observational, not part of the round contract.
        """
        key = _key(owner, host, "round", round_id)
        with self.engine.begin() as connection:
            current = self._read(connection, key)
            if current is None or current.state in {"blocked", "cancelled"}:
                return None
            merged = {**current.payload, "advisor_overhead": dict(overhead)}
            return self._cas(connection, key, current, current.state, merged)
