"""Host identity collision checks shared by tunnel admission."""

from __future__ import annotations

from omnigent.stores.host_store import Host, HostStore, host_is_live


def find_live_host_name_collision(
    host_store: HostStore,
    *,
    host_id: str,
    user_id: str,
    name: str,
) -> Host | None:
    """Return a different live host already using ``(user_id, name)``.

    Reconnects with the same persistent ID remain valid. Offline or stale
    registrations may continue through the existing identity-rotation path.
    """
    for existing in host_store.list_hosts(user_id):
        if existing.name == name and existing.host_id != host_id and host_is_live(existing):
            return existing
    return None
