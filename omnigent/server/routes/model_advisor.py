"""Authenticated HTTP surface for the concrete model advisor.

Mounted only when the ``model_advisor`` release feature is enabled, so the
API is invisible (404) while the gate is off. Every route derives the owner
from authentication, verifies host ownership, and projects public DTOs —
the private frozen round snapshot never reaches a browser.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from omnigent.model_advisor_core import AdvisorContractError
from omnigent.model_advisor_workflow import AdvisorPreferences
from omnigent.server.auth import AuthProvider
from omnigent.server.model_advisor_service import ModelAdvisorService
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin

_JSON_MUTATION_GUARDS = [
    Depends(require_json_content_type),
    Depends(require_trusted_origin),
]


class AdvisorPreferencesBody(BaseModel):
    """Client-supplied preference payload; ids only, never route metadata."""

    enabled: bool
    allowed_candidate_ids: list[str] = Field(max_length=128)
    advisor_candidate_id: str | None = None
    human_probability_percent: int = Field(ge=0, le=100, default=50)


class SavePreferencesRequest(BaseModel):
    host_id: str
    profile: str = "default"
    preferences: AdvisorPreferencesBody
    expected_version: int = Field(ge=0)


class CreateRoundRequest(BaseModel):
    host_id: str
    profile: str = "default"
    task: str = Field(min_length=1, max_length=200_000)
    human_candidate_id: str


class SessionLaunchRequest(BaseModel):
    """Where/how to run the round; never which treatment is assigned."""

    agent_id: str
    workspace: str
    terminal_launch_args: list[str] | None = Field(default=None, max_length=16)


class ConfirmRoundRequest(BaseModel):
    host_id: str
    expected_version: int = Field(ge=0)
    override_candidate_id: str | None = None
    reason: str | None = Field(default=None, max_length=600)
    launch: SessionLaunchRequest


class CancelRoundRequest(BaseModel):
    host_id: str
    expected_version: int = Field(ge=0)


def create_model_advisor_router(
    *,
    auth_provider: AuthProvider | None = None,
    service: ModelAdvisorService,
) -> APIRouter:
    """Create the feature-gated model advisor router."""
    router = APIRouter()

    def service_for(request: Request) -> ModelAdvisorService:
        require_user(request, auth_provider)
        return service

    def _preferences_from_body(body: AdvisorPreferencesBody) -> AdvisorPreferences:
        try:
            return AdvisorPreferences(
                enabled=body.enabled,
                allowed_candidate_ids=tuple(body.allowed_candidate_ids),
                advisor_candidate_id=body.advisor_candidate_id,
                human_probability_percent=body.human_probability_percent,
            )
        except AdvisorContractError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @router.get("/model-advisor/catalog")
    async def get_catalog(request: Request, host_id: str) -> dict[str, Any]:
        """Project the live qualified choice set for one host."""
        user = require_user(request, auth_provider)
        catalog = await service.load_catalog(user, host_id)
        return {
            "object": "model_advisor.catalog",
            "catalog_revision": catalog.catalog_revision,
            "options": [
                {
                    "candidate_id": option.candidate_id,
                    "model_id": option.model_id,
                    "display_name": option.display_name,
                    "lane_id": option.lane_id,
                    "reasoning_effort": option.reasoning_effort,
                    "access_class": option.access_class,
                    "is_default_effort": option.is_default_effort,
                }
                for option in catalog.options
            ],
        }

    @router.get("/model-advisor/preferences")
    async def get_preferences(
        request: Request,
        host_id: str,
        profile: str = "default",
    ) -> dict[str, Any]:
        user = require_user(request, auth_provider)
        result = await service.load_preferences(user, host_id, profile)
        if result is None:
            return {
                "object": "model_advisor.preferences",
                "version": 0,
                "etag": None,
                "state": "unsaved",
                "preferences": None,
            }
        return result

    @router.put(
        "/model-advisor/preferences",
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def put_preferences(
        request: Request,
        body: SavePreferencesRequest,
    ) -> dict[str, Any]:
        user = require_user(request, auth_provider)
        return await service.save_preferences(
            user,
            body.host_id,
            body.profile,
            _preferences_from_body(body.preferences),
            expected_version=body.expected_version,
        )

    @router.post(
        "/model-advisor/rounds",
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def post_round(request: Request, body: CreateRoundRequest) -> dict[str, Any]:
        """Freeze and reserve a round; the advisor call runs in background.

        A duplicate submission (same round reservation won twice) joins the
        existing reservation instead of starting a second advisor call; the
        returned ``state`` and the round id tell the client which happened.
        """
        user = require_user(request, auth_provider)
        return await service.create_round(
            user,
            body.host_id,
            body.profile,
            task=body.task,
            human_candidate_id=body.human_candidate_id,
        )

    @router.get("/model-advisor/rounds/{round_id}")
    async def get_round(
        request: Request,
        round_id: str,
        host_id: str,
    ) -> dict[str, Any]:
        user = require_user(request, auth_provider)
        return await service.load_round(user, host_id, round_id)

    @router.post(
        "/model-advisor/rounds/{round_id}/confirm",
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def post_confirm(
        request: Request,
        round_id: str,
        body: ConfirmRoundRequest,
    ) -> dict[str, Any]:
        """Confirm or explicitly override, then launch the one session."""
        user = require_user(request, auth_provider)
        return await service.confirm_round(
            user,
            body.host_id,
            round_id,
            expected_version=body.expected_version,
            override_candidate_id=body.override_candidate_id,
            reason=body.reason,
            launch=body.launch.model_dump(),
            request=request,
        )

    @router.post(
        "/model-advisor/rounds/{round_id}/cancel",
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def post_cancel(
        request: Request, round_id: str, body: CancelRoundRequest
    ) -> dict[str, Any]:
        user = require_user(request, auth_provider)
        return await service.cancel_round(
            user,
            body.host_id,
            round_id,
            expected_version=body.expected_version,
        )

    return router
