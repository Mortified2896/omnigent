"""End-to-end tunnel admission coverage for live host identity collisions."""

from __future__ import annotations

from asgiref.testing import ApplicationCommunicator
from fastapi import FastAPI

from omnigent.host.frames import HostHelloFrame, encode_host_frame
from omnigent.server.host_registry import HostRegistry
from omnigent.server.routes.host_tunnel import create_host_tunnel_router
from omnigent.stores.host_store import HostStore

_EXISTING_HOST_ID = "ed5ddb6695a84c3091d8925b7394a6c4"
_CONNECTING_HOST_ID = "07e100efd05744b593285539f0e985ec"
_NAME = "ai-control-hub"
_OWNER = "local"


def _websocket_scope(path: str) -> dict[str, object]:
    return {
        "type": "websocket",
        "asgi": {"version": "3.0"},
        "scheme": "ws",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 50000),
        "server": ("testserver", 80),
        "subprotocols": [],
    }


def _hello() -> str:
    return encode_host_frame(
        HostHelloFrame(
            version="0.9.0-test",
            frame_protocol_version=1,
            name=_NAME,
            runners=[],
        )
    )


async def test_tunnel_rejects_live_owner_name_collision(db_uri: str) -> None:
    """A stray live daemon cannot rotate the real host registration away."""
    registry = HostRegistry()
    store = HostStore(db_uri)
    store.upsert_on_connect(
        host_id=_EXISTING_HOST_ID,
        name=_NAME,
        user_id=_OWNER,
    )

    app = FastAPI()
    app.include_router(create_host_tunnel_router(registry, store), prefix="/v1")
    path = f"/v1/hosts/{_CONNECTING_HOST_ID}/tunnel"
    communicator = ApplicationCommunicator(app, _websocket_scope(path))

    await communicator.send_input({"type": "websocket.connect"})
    accepted = await communicator.receive_output(timeout=1.0)
    assert accepted["type"] == "websocket.accept"

    await communicator.send_input({"type": "websocket.receive", "text": _hello()})
    refused = await communicator.receive_output(timeout=1.0)
    assert refused["type"] == "websocket.close"
    assert refused["code"] == 4009
    assert "Another live host" in refused["reason"]

    existing = store.get_host(_EXISTING_HOST_ID)
    assert existing is not None
    assert existing.name == _NAME
    assert existing.status == "online"
    assert store.get_host(_CONNECTING_HOST_ID) is None
    assert registry.get(_CONNECTING_HOST_ID) is None
