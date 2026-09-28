"""Small data contracts shared by a deployment controller and Omnigent.

These values describe evidence only. They grant no filesystem, process or
service-control capability.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class ComponentObservation:
    """One runtime component's generation-bound work observation."""

    name: str
    generation: str
    active_work: int | None
    status: str = "known"

    def to_dict(self) -> dict[str, Any]:
        """Return the wire representation."""
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ComponentObservation:
        """Parse a wire representation without coercing unknown values."""
        name = value.get("name")
        generation = value.get("generation")
        active_work = value.get("active_work")
        status = value.get("status", "known")
        if not isinstance(name, str) or not isinstance(generation, str):
            raise ValueError("component observation identity is invalid")
        if active_work is not None and type(active_work) is not int:
            raise ValueError("component active-work count is invalid")
        if not isinstance(status, str):
            raise ValueError("component observation status is invalid")
        return cls(name, generation, active_work, status)


@dataclass(frozen=True)
class QuiescenceCertificate:
    """Short-lived proof that one fenced process observed zero admitted work."""

    certificate_id: str
    fence_generation: int
    process_generation: str
    activity_generation: int
    state_identity: str
    state_generation: str
    persistent_state_digest: str
    observed_at: float
    components: tuple[ComponentObservation, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON-compatible certificate representation."""
        return {
            "certificate_id": self.certificate_id,
            "fence_generation": self.fence_generation,
            "process_generation": self.process_generation,
            "activity_generation": self.activity_generation,
            "state_identity": self.state_identity,
            "state_generation": self.state_generation,
            "persistent_state_digest": self.persistent_state_digest,
            "observed_at": self.observed_at,
            "components": [item.to_dict() for item in self.components],
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> QuiescenceCertificate:
        """Parse a certificate returned by the fixed controller protocol."""
        components = value.get("components")
        if not isinstance(components, list):
            raise ValueError("quiescence certificate components are invalid")
        integer_fields = ("fence_generation", "activity_generation")
        if any(type(value.get(field)) is not int for field in integer_fields):
            raise ValueError("quiescence certificate generation is invalid")
        observed_at = value.get("observed_at")
        if type(observed_at) is int:
            observed_timestamp = float(observed_at)
        elif type(observed_at) is float:
            observed_timestamp = observed_at
        else:
            raise ValueError("quiescence certificate timestamp is invalid")
        string_fields = (
            "certificate_id",
            "process_generation",
            "state_identity",
            "state_generation",
            "persistent_state_digest",
        )
        if any(not isinstance(value.get(field), str) for field in string_fields):
            raise ValueError("quiescence certificate identity is invalid")
        return cls(
            certificate_id=value["certificate_id"],
            fence_generation=value["fence_generation"],
            process_generation=value["process_generation"],
            activity_generation=value["activity_generation"],
            state_identity=value["state_identity"],
            state_generation=value["state_generation"],
            persistent_state_digest=value["persistent_state_digest"],
            observed_at=observed_timestamp,
            components=tuple(ComponentObservation.from_dict(item) for item in components),
        )
