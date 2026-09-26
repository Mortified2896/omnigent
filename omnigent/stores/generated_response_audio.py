"""SQLAlchemy persistence for generated audio tied to exact responses."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import asc, select, update
from sqlalchemy.exc import IntegrityError

from omnigent.db.db_models import (
    DEFAULT_WORKSPACE_ID,
    SqlGeneratedResponseAudio,
    current_workspace_id,
)
from omnigent.db.utils import get_or_create_engine, make_named_managed_session_maker, now_epoch


@dataclass(frozen=True)
class GeneratedResponseAudio:
    workspace_id: int
    conversation_id: str
    response_id: str
    status: str
    voice_profile: str
    artifact_key: str | None
    duration_seconds: float | None
    sample_rate: int | None
    error_code: str | None
    updated_at: int


def _to_entity(row: SqlGeneratedResponseAudio) -> GeneratedResponseAudio:
    return GeneratedResponseAudio(
        workspace_id=row.workspace_id or DEFAULT_WORKSPACE_ID,
        conversation_id=row.conversation_id,
        response_id=row.response_id,
        status=row.status,
        voice_profile=row.voice_profile,
        artifact_key=row.artifact_key,
        duration_seconds=row.duration_seconds,
        sample_rate=row.sample_rate,
        error_code=row.error_code,
        updated_at=row.updated_at,
    )


class SqlAlchemyGeneratedResponseAudioStore:
    """Persists generation state; WAV bytes stay in the ArtifactStore."""

    def __init__(self, storage_location: str) -> None:
        self.storage_location = storage_location
        self._engine = get_or_create_engine(storage_location)
        self._session = make_named_managed_session_maker(
            self._engine,
            query_name_prefix="omnigent.generated_response_audio",
        )

    def create_pending(
        self,
        conversation_id: str,
        response_id: str,
        voice_profile: str,
    ) -> GeneratedResponseAudio:
        row = SqlGeneratedResponseAudio(
            workspace_id=current_workspace_id(),
            conversation_id=conversation_id,
            response_id=response_id,
            status="pending",
            voice_profile=voice_profile,
            artifact_key=None,
            duration_seconds=None,
            sample_rate=None,
            error_code=None,
            updated_at=now_epoch(),
        )
        try:
            with self._session("insert_generated_audio") as session:
                session.add(row)
                session.flush()
                return _to_entity(row)
        except IntegrityError:
            existing = self.get(conversation_id, response_id)
            if existing is None:
                raise
            return existing

    def get(self, conversation_id: str, response_id: str) -> GeneratedResponseAudio | None:
        with self._session("get_generated_audio") as session:
            row = session.get(
                SqlGeneratedResponseAudio,
                (current_workspace_id(), conversation_id, response_id),
            )
            return _to_entity(row) if row is not None else None

    def list_for_conversation(self, conversation_id: str) -> list[GeneratedResponseAudio]:
        with self._session("list_generated_audio") as session:
            rows = (
                session.execute(
                    select(SqlGeneratedResponseAudio)
                    .where(SqlGeneratedResponseAudio.workspace_id == current_workspace_id())
                    .where(SqlGeneratedResponseAudio.conversation_id == conversation_id)
                    .order_by(
                        asc(SqlGeneratedResponseAudio.updated_at),
                        asc(SqlGeneratedResponseAudio.response_id),
                    )
                )
                .scalars()
                .all()
            )
            return [_to_entity(row) for row in rows]

    def claim_pending(self, conversation_id: str, response_id: str) -> bool:
        with self._session("claim_generated_audio") as session:
            result = session.execute(
                update(SqlGeneratedResponseAudio)
                .where(SqlGeneratedResponseAudio.workspace_id == current_workspace_id())
                .where(SqlGeneratedResponseAudio.conversation_id == conversation_id)
                .where(SqlGeneratedResponseAudio.response_id == response_id)
                .where(SqlGeneratedResponseAudio.status == "pending")
                .values(status="processing", updated_at=now_epoch())
            )
            return result.rowcount == 1

    def recover_processing(self) -> int:
        """Recover only jobs stale for 30 minutes after a worker/server crash.

        The processing heartbeat is written when a job is claimed. A bounded
        stale window prevents a second replica starting during a long synthesis
        from resetting another replica's active job and generating a duplicate.
        """
        stale_before = now_epoch() - 30 * 60
        with self._session("recover_generated_audio") as session:
            result = session.execute(
                update(SqlGeneratedResponseAudio)
                .where(SqlGeneratedResponseAudio.status == "processing")
                .where(SqlGeneratedResponseAudio.updated_at < stale_before)
                .values(status="pending", updated_at=now_epoch())
            )
            return int(result.rowcount or 0)

    def list_pending_all_workspaces(self) -> list[GeneratedResponseAudio]:
        with self._session("list_pending_generated_audio") as session:
            rows = (
                session.execute(
                    select(SqlGeneratedResponseAudio)
                    .where(SqlGeneratedResponseAudio.status == "pending")
                    .order_by(
                        asc(SqlGeneratedResponseAudio.workspace_id),
                        asc(SqlGeneratedResponseAudio.updated_at),
                    )
                )
                .scalars()
                .all()
            )
            return [_to_entity(row) for row in rows]

    def mark_ready(
        self,
        conversation_id: str,
        response_id: str,
        *,
        artifact_key: str,
        duration_seconds: float,
        sample_rate: int,
    ) -> bool:
        with self._session("complete_generated_audio") as session:
            result = session.execute(
                update(SqlGeneratedResponseAudio)
                .where(SqlGeneratedResponseAudio.workspace_id == current_workspace_id())
                .where(SqlGeneratedResponseAudio.conversation_id == conversation_id)
                .where(SqlGeneratedResponseAudio.response_id == response_id)
                .where(SqlGeneratedResponseAudio.status == "processing")
                .values(
                    status="ready",
                    artifact_key=artifact_key,
                    duration_seconds=duration_seconds,
                    sample_rate=sample_rate,
                    error_code=None,
                    updated_at=now_epoch(),
                )
            )
            return result.rowcount == 1

    def mark_failed(self, conversation_id: str, response_id: str, error_code: str) -> bool:
        with self._session("fail_generated_audio") as session:
            result = session.execute(
                update(SqlGeneratedResponseAudio)
                .where(SqlGeneratedResponseAudio.workspace_id == current_workspace_id())
                .where(SqlGeneratedResponseAudio.conversation_id == conversation_id)
                .where(SqlGeneratedResponseAudio.response_id == response_id)
                .where(SqlGeneratedResponseAudio.status.in_(("pending", "processing")))
                .values(status="failed", error_code=error_code[:64], updated_at=now_epoch())
            )
            return result.rowcount == 1
