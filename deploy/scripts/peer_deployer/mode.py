"""Explicit deployment modes for peer-supervised promotion."""

from __future__ import annotations

from enum import Enum


class DeploymentMode(str, Enum):
    """How the accepted candidate reaches the target.

    ``BOOTSTRAP_FIRST_PEER`` activates an already accepted, immutable
    candidate on the first peer. ``PEER_COPY`` copies that exact candidate
    from the healthy peer after proving the peer is running it.
    """

    BOOTSTRAP_FIRST_PEER = "bootstrap-first-peer"
    PEER_COPY = "peer-copy"

    @classmethod
    def parse(cls, value: str | DeploymentMode) -> DeploymentMode:
        if isinstance(value, cls):
            return value
        try:
            return cls(value)
        except ValueError as exc:
            valid = ", ".join(item.value for item in cls)
            raise ValueError(f"unknown deployment mode {value!r}; valid: {valid}") from exc


__all__ = ["DeploymentMode"]
