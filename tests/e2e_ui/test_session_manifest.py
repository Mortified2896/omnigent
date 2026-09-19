"""Unit tests for manifest-backed test-session teardown safety."""

from __future__ import annotations

from types import SimpleNamespace

import tests.e2e_ui.conftest as e2e_conftest
from omnigent.util.test_session_policy import test_session_labels as _test_session_labels


def _request_for_passed_test() -> SimpleNamespace:
    """Return the small pytest request shape used by the finalizer."""
    return SimpleNamespace(
        node=SimpleNamespace(
            rep_call=SimpleNamespace(outcome="passed", longreprtext=""),
        )
    )


def _manifest(tmp_path, base_url: str, session_id: str) -> object:
    """Create a manifest whose server identity matches the fixture globals."""
    e2e_conftest._server_state.clear()
    e2e_conftest._server_state.update(
        {
            "server_url": base_url,
            "pid": 123,
            "runner_id": "runner-test",
            "database_uri": "sqlite:///private-test.db",
        }
    )
    manifest = e2e_conftest._TestSessionManifest(
        path=tmp_path / "creation-manifest.json",
        server_instance={
            "server_url": base_url,
            "pid": 123,
            "runner_id": "runner-test",
            "database_uri": "sqlite:///private-test.db",
        },
        run_id="run-test",
        creator="codex",
    )
    manifest.record_created(session_id)
    return manifest


def test_finalizer_preserves_when_snapshot_is_newer_than_verified_baseline(
    tmp_path,
    monkeypatch,
) -> None:
    """A human edit before the finalizer GET cannot become its new baseline."""
    base_url = "http://test-server"
    session_id = "conv_changed"
    manifest = _manifest(tmp_path, base_url, session_id)
    manifest.record_verified(session_id, '"verified-before-edit"')
    delete_calls: list[object] = []

    monkeypatch.setattr(
        e2e_conftest.httpx,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            headers={"etag": '"human-edited"'},
            json={},
        ),
    )
    monkeypatch.setattr(
        e2e_conftest.httpx,
        "delete",
        lambda *args, **kwargs: delete_calls.append((args, kwargs)),
    )

    e2e_conftest._finalize_test_session(
        base_url,
        session_id,
        manifest,
        _request_for_passed_test(),
    )

    assert delete_calls == []
    assert manifest.decisions[-1] == {
        "session_id": session_id,
        "action": "preserve",
        "reason": "Session changed after the last harness-verified snapshot",
    }


def test_finalizer_preserves_after_conditional_delete_race(
    tmp_path,
    monkeypatch,
) -> None:
    """A mutation after validation yields preservation without a fallback delete."""
    base_url = "http://test-server"
    session_id = "conv_raced"
    manifest = _manifest(tmp_path, base_url, session_id)
    verified_etag = '"verified"'
    manifest.record_verified(session_id, verified_etag)
    get_calls: list[object] = []
    delete_calls: list[object] = []

    snapshot = {
        "id": session_id,
        "labels": _test_session_labels(manifest.run_id, manifest.creator),
        "status": "idle",
        "parent_session_id": None,
    }

    def fake_get(*args, **kwargs):
        get_calls.append((args, kwargs))
        if len(get_calls) == 1:
            return SimpleNamespace(
                status_code=200,
                headers={"etag": verified_etag},
                json=lambda: dict(snapshot),
            )
        return SimpleNamespace(status_code=200, json=lambda: {"data": []})

    monkeypatch.setattr(e2e_conftest.httpx, "get", fake_get)
    monkeypatch.setattr(
        e2e_conftest.httpx,
        "delete",
        lambda *args, **kwargs: (
            delete_calls.append((args, kwargs))
            or SimpleNamespace(status_code=412)
        ),
    )

    e2e_conftest._finalize_test_session(
        base_url,
        session_id,
        manifest,
        _request_for_passed_test(),
    )

    assert len(delete_calls) == 1
    assert delete_calls[0][1]["headers"] == {"If-Match": verified_etag}
    assert manifest.decisions[-1] == {
        "session_id": session_id,
        "action": "preserve",
        "reason": "Conditional delete fence rejected a concurrent change",
    }
