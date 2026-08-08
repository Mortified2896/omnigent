"""Host identity collision checks shared by the tunnel admission path.

A host's persistent ``host_id`` may legitimately rotate after its local
identity file is regenerated. Rotation is safe only when the previous
registration for the same owner/name identity is no longer live. If two live
daemons advertise the same owner/name under different ids, allowing the normal
rotation path makes them replace each other in the database and routes control
traffic to whichever daemon connected most recently.
"""

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

    Reconnects with the same persistent ``host_id`` are always allowed. A
    different id is considered a collision only while the prior row is both
    online and heartbeat-fresh according to :func:`host_is_live`; an offline or
    stale row may therefore continue through the existing identity-rotation
    path.

    ``HostStore.list_hosts`` is already scoped to the current workspace, so the
    identity boundary here is effectively ``(workspace, user_id, name)``.

    :param host_store: Persistent host registration store.
    :param host_id: Canonical id of the connecting host.
    :param user_id: Authenticated owner of the connecting host.
    :param name: Name advertised by the connecting host in ``host.hello``.
    :returns: The conflicting live host, or ``None`` when admission/rotation is
        safe.
    """
    for existing in host_store.list_hosts(user_id):
        if existing.name == name and existing.host_id != host_id and host_is_live(existing):
            return existing
    return None
