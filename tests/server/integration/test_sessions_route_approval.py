"""Integration tests for the session-scoped ``route_approval_enabled`` column.

Mirrors ``test_sessions_cost_control_override.py``:
PATCH writes the column, the snapshot reads it back, and
create-time values land before the first turn. The clearing
contract mirrors ``model_override``'s explicit-null pattern so
the toggle survives unrelated PATCHes (renames, runner binds).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


async def _create_session(
    client: httpx.AsyncClient,
    agent_id: str,
) -> dict[str, Any]:
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id, "initial_items": []},
    )
    assert resp.status_code == 201
    return resp.json()


@pytest.mark.parametrize("enabled", [True, False])
async def test_patch_route_approval_enabled_round_trips_through_snapshot(
    client: httpx.AsyncClient,
    enabled: bool,
) -> None:
    """PATCH writes the column and ``GET`` returns the same value."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    assert session.get("route_approval_enabled") is None

    patch = await client.patch(
        f"/v1/sessions/{sid}",
        json={"route_approval_enabled": enabled},
    )
    assert patch.status_code == 200, patch.text
    assert patch.json()["route_approval_enabled"] is enabled

    get = await client.get(f"/v1/sessions/{sid}")
    assert get.status_code == 200
    assert get.json()["route_approval_enabled"] is enabled


async def test_patch_route_approval_explicit_null_clears(
    client: httpx.AsyncClient,
) -> None:
    """An explicit JSON ``null`` clears the override back to unset."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    seed = await client.patch(
        f"/v1/sessions/{sid}",
        json={"route_approval_enabled": True},
    )
    assert seed.json()["route_approval_enabled"] is True

    clear = await client.patch(
        f"/v1/sessions/{sid}",
        json={"route_approval_enabled": None},
    )
    assert clear.status_code == 200, clear.text
    assert clear.json()["route_approval_enabled"] is None

    get = await client.get(f"/v1/sessions/{sid}")
    assert get.json()["route_approval_enabled"] is None


async def test_patch_without_field_leaves_route_approval_unchanged(
    client: httpx.AsyncClient,
) -> None:
    """An unrelated PATCH must not silently reset the toggle."""
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    await client.patch(
        f"/v1/sessions/{sid}",
        json={"route_approval_enabled": True},
    )

    rename = await client.patch(
        f"/v1/sessions/{sid}",
        json={"title": "unrelated rename"},
    )
    assert rename.status_code == 200, rename.text
    assert rename.json()["route_approval_enabled"] is True


async def test_patch_route_approval_rejects_non_boolean(
    client: httpx.AsyncClient,
) -> None:
    """Non-boolean values fail loud with 400.

    The persisted value gates the execution gate, so a typo must
    not silently persist.
    """
    agent = await create_test_agent(client)
    session = await _create_session(client, agent["id"])
    sid = session["id"]

    # Numeric 0/1 are not booleans — they must be rejected instead of
    # coerced silently (Pydantic would coerce the string "yes" to
    # True; we use a number here so the validation path is exercised).
    resp = await client.patch(
        f"/v1/sessions/{sid}",
        json={"route_approval_enabled": 1},
    )
    assert resp.status_code == 422, (
        f"route_approval_enabled should 422, got {resp.status_code}: {resp.text}"
    )

    get = await client.get(f"/v1/sessions/{sid}")
    assert get.json()["route_approval_enabled"] is None


@pytest.mark.parametrize("enabled", [True, False])
async def test_create_session_with_route_approval_enabled_persists(
    client: httpx.AsyncClient,
    enabled: bool,
) -> None:
    """Create-time toggle lands on the row and the snapshot."""
    agent = await create_test_agent(client)
    resp = await client.post(
        "/v1/sessions",
        json={
            "agent_id": agent["id"],
            "initial_items": [],
            "route_approval_enabled": enabled,
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created.get("route_approval_enabled") is enabled

    get = await client.get(f"/v1/sessions/{created['id']}")
    assert get.json()["route_approval_enabled"] is enabled