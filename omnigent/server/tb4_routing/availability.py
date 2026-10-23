"""Thin client for the shared OmniRoute availability service; no local scan runner."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.o3_routing_review.omniroute import OmniRouteClient, OmniRouteError


class ProbePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    cost_mode: Literal["free", "included", "metered"] = "included"
    minimum_score: float = Field(default=0, ge=0, le=100)
    include_unscored: bool = True
    max_input_usd_per_million: float = Field(default=1, ge=0, le=1000)
    max_output_usd_per_million: float = Field(default=5, ge=0, le=10000)
    estimated_budget_usd: float = Field(default=1, gt=0, le=10)
    retest: bool = False


async def gateway_request(method: str, path: str = "", body: dict | None = None) -> dict:
    try:
        response = await OmniRouteClient.from_env()._request(
            method, "/api/model-availability" + path, body=body, timeout=30
        )
        data = response.body
        if not isinstance(data, dict):
            raise ValueError("Invalid gateway response")
        if path in ("", "/refresh", "/selection") and (
            data.get("authority") != "omniroute" or data.get("protocol_version") != 1
        ):
            raise ValueError("Unknown authority")
        return dict(data)
    except (OmniRouteError, ValueError, OSError) as exc:
        raise OmnigentError(
            "OmniRoute availability is unavailable. Open OmniRoute for scan status; "
            "no local fallback was started.",
            code=ErrorCode.CONFLICT,
        ) from exc
