"""Narrow client for the host-owned RTX deployment controller.

The web process is intentionally not a deployer.  It can ask the independent
controller for a read-only plan, enqueue one fixed O1 -> O2 operation, and
read an opaque job status.  It cannot submit release identities, artifacts,
paths, commands, health evidence, or capability flags.
"""

from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any, Protocol

CONTROLLER_SOCKET = Path("/run/omnigent-peer-controller/controller.sock")
MAX_RESPONSE_BYTES = 1024 * 1024


class DeploymentControllerError(RuntimeError):
    """A controller request was rejected or returned an invalid response."""


class DeploymentControllerUnavailable(DeploymentControllerError):
    """The host-owned controller is not installed or cannot be reached."""


class DeploymentControllerClient(Protocol):
    """The minimal interface used by the FastAPI route and its tests."""

    def request(self, payload: dict[str, Any]) -> dict[str, Any]: ...


class UnixSocketDeploymentControllerClient:
    """Call the fixed, host-owned Unix socket using one JSON-lines request."""

    def __init__(
        self,
        *,
        socket_path: Path = CONTROLLER_SOCKET,
        timeout_seconds: float = 0.75,
    ) -> None:
        self._socket_path = socket_path
        self._timeout_seconds = timeout_seconds

    def request(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or not isinstance(payload.get("operation"), str):
            raise DeploymentControllerError("invalid controller operation")
        encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(self._timeout_seconds)
                connection.connect(str(self._socket_path))
                connection.sendall(encoded)
                response = bytearray()
                while len(response) < MAX_RESPONSE_BYTES:
                    chunk = connection.recv(min(65536, MAX_RESPONSE_BYTES - len(response)))
                    if not chunk:
                        break
                    response.extend(chunk)
                    if b"\n" in chunk:
                        break
        except (OSError, TimeoutError) as exc:
            raise DeploymentControllerUnavailable("deployment controller unavailable") from exc
        try:
            value = json.loads(bytes(response).splitlines()[0])
        except (IndexError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise DeploymentControllerError("invalid deployment controller response") from exc
        if not isinstance(value, dict):
            raise DeploymentControllerError("invalid deployment controller response")
        if value.get("ok") is not True:
            message = value.get("message")
            raise DeploymentControllerError(
                message if isinstance(message, str) and message else "deployment request rejected"
            )
        return value
