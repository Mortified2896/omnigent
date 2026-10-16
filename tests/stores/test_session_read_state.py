"""Read receipts survive restarts, concurrent writers, and workspace boundaries."""

from concurrent.futures import ThreadPoolExecutor

from omnigent.db.db_models import workspace_scope
from omnigent.stores.session_read_state import SessionReadStateStore


def test_receipts_survive_a_fresh_store_and_keep_users_separate(db_uri: str) -> None:
    store = SessionReadStateStore(db_uri)
    version = store.put(None, "chat", 200, False)
    restarted = SessionReadStateStore(db_uri)
    assert restarted.get_many(None, ["chat"]) == {"chat": (200, False, version)}
    assert restarted.get_many("bob", ["chat"]) == {}
    with workspace_scope(42):
        assert restarted.get_many(None, ["chat"]) == {}


def test_delayed_receipt_cannot_undo_newer_read(db_uri: str) -> None:
    store = SessionReadStateStore(db_uri)
    version = store.put(None, "chat", 500, False)
    assert store.put(None, "chat", 100, False) == version
    assert store.get_many(None, ["chat"])["chat"] == (500, False, version)


def test_intentional_unread_and_read_each_get_a_new_revision(db_uri: str) -> None:
    store = SessionReadStateStore(db_uri)
    read_version = store.put(None, "chat", 500, False)
    unread_version = store.put(None, "chat", 199, True)
    assert unread_version > read_version
    assert store.get_many(None, ["chat"])["chat"] == (199, True, unread_version)
    assert store.put(None, "chat", 200, False) > unread_version


def test_concurrent_first_receipts_keep_the_furthest_read(db_uri: str) -> None:
    stores = [SessionReadStateStore(db_uri) for _ in range(3)]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures = [
            pool.submit(store.put, None, "chat", seen, False)
            for store, seen in zip(stores, [100, 500, 200], strict=True)
        ]
        for future in futures:
            future.result()
    assert stores[0].get_many(None, ["chat"])["chat"][:2] == (500, False)


def test_prune_removes_only_the_target_session_and_workspace(db_uri: str) -> None:
    store = SessionReadStateStore(db_uri)
    previous_version = store.put(None, "chat", 200, False)
    store.put("alice", "chat", 100, True)
    store.put(None, "other", 300, False)
    with workspace_scope(42):
        store.put(None, "chat", 400, False)
    store.prune("chat")
    assert store.get_many(None, ["chat"]) == {}
    assert store.get_many("alice", ["chat"]) == {}
    assert store.get_many(None, ["other"])["other"][0] == 300
    with workspace_scope(42):
        assert store.get_many(None, ["chat"])["chat"][0] == 400
    assert store.put(None, "chat", 500, False) > previous_version
