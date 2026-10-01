"""Exercise the audio UPDATE result contract with real SQLAlchemy results."""

from __future__ import annotations

from collections.abc import Iterator
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, select, update
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column

from omnigent.stores.generated_response_audio import _affected_rows


class _Base(DeclarativeBase):
    pass


class _AudioRow(_Base):
    __tablename__ = "audio_rowcount_test"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[str]


@pytest.fixture
def audio_session() -> Iterator[Session]:
    engine = create_engine("sqlite://")
    _Base.metadata.create_all(engine)
    try:
        with Session(engine) as session:
            session.add_all([_AudioRow(id=1, status="pending"), _AudioRow(id=2, status="pending")])
            session.commit()
            yield session
    finally:
        engine.dispose()


def test_update_rowcount_distinguishes_claim_from_replay(audio_session: Session) -> None:
    statement = (
        update(_AudioRow)
        .where(_AudioRow.id == 1, _AudioRow.status == "pending")
        .values(status="processing")
    )
    assert _affected_rows(audio_session.execute(statement)) == 1
    assert _affected_rows(audio_session.execute(statement)) == 0


def test_update_rowcount_preserves_multiple_rows(audio_session: Session) -> None:
    result = audio_session.execute(update(_AudioRow).values(status="processing"))
    assert _affected_rows(result) == 2


@pytest.mark.parametrize("result", [object(), SimpleNamespace(rowcount=1)])
def test_non_cursor_result_is_not_silently_treated_as_zero(result: object) -> None:
    with pytest.raises(TypeError, match="CursorResult"):
        _affected_rows(result)


def test_select_result_is_not_an_update(audio_session: Session) -> None:
    with pytest.raises(TypeError, match="CursorResult"):
        _affected_rows(audio_session.execute(select(_AudioRow)))


def test_unknown_rowcount_raises_and_allows_transaction_rollback(
    audio_session: Session, monkeypatch: pytest.MonkeyPatch
) -> None:
    with pytest.raises(RuntimeError, match="unknown row count"), audio_session.begin():
        result = audio_session.execute(update(_AudioRow).values(status="processing"))
        monkeypatch.setattr(result, "rowcount", -1)
        _affected_rows(result)
    assert audio_session.scalars(select(_AudioRow.status)).all() == ["pending", "pending"]
