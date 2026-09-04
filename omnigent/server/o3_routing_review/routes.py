"""Authenticated HTTP surface for the O3 pre-session routing review."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin

from .models import (
    CleanupResult,
    ProposalAdjustmentRequest,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalOutcomeRequest,
    ProposalSessionLinkRequest,
    RoutingProposal,
)
from .registry import SOURCE_POOL_NAME
from .service import O3RoutingReviewService, get_o3_routing_review_service

_JSON_MUTATION_GUARDS = [
    Depends(require_json_content_type),
    Depends(require_trusted_origin),
]


def create_o3_routing_review_router(
    *,
    auth_provider: AuthProvider | None = None,
    service_factory: Callable[[], O3RoutingReviewService] = get_o3_routing_review_service,
) -> APIRouter:
    """Create the feature-gated O3 routing-review router."""
    router = APIRouter()

    def service_for(request: Request) -> O3RoutingReviewService:
        require_user(request, auth_provider)
        return service_factory()

    @router.get("/o3/routing-review/registry")
    async def get_registry(request: Request) -> dict[str, object]:
        service = service_for(request)
        return {
            "source_pool": SOURCE_POOL_NAME,
            "slices": [item.model_dump(mode="json") for item in service.registry.slices],
        }

    @router.post(
        "/o3/routing-review/proposals",
        response_model=RoutingProposal,
        status_code=201,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def create_proposal(
        request: Request,
        body: ProposalCreateRequest,
    ) -> RoutingProposal:
        return await service_for(request).create_proposal(body)

    @router.get(
        "/o3/routing-review/proposals/{proposal_id}",
        response_model=RoutingProposal,
    )
    async def get_proposal(request: Request, proposal_id: str) -> RoutingProposal:
        return service_for(request).get_proposal(proposal_id)

    @router.patch(
        "/o3/routing-review/proposals/{proposal_id}",
        response_model=RoutingProposal,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def adjust_proposal(
        request: Request,
        proposal_id: str,
        body: ProposalAdjustmentRequest,
    ) -> RoutingProposal:
        return await service_for(request).adjust_proposal(proposal_id, body)

    @router.post(
        "/o3/routing-review/proposals/{proposal_id}/decision",
        response_model=RoutingProposal,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def decide_proposal(
        request: Request,
        proposal_id: str,
        body: ProposalDecisionRequest,
    ) -> RoutingProposal:
        return await service_for(request).decide_proposal(proposal_id, body)

    @router.post(
        "/o3/routing-review/proposals/{proposal_id}/session",
        response_model=RoutingProposal,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def link_session(
        request: Request,
        proposal_id: str,
        body: ProposalSessionLinkRequest,
    ) -> RoutingProposal:
        return service_for(request).link_session(proposal_id, body)

    @router.post(
        "/o3/routing-review/proposals/{proposal_id}/outcome",
        response_model=RoutingProposal,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def record_outcome(
        request: Request,
        proposal_id: str,
        body: ProposalOutcomeRequest,
    ) -> RoutingProposal:
        return service_for(request).record_outcome(proposal_id, body)

    @router.post(
        "/o3/routing-review/cleanup",
        response_model=CleanupResult,
        dependencies=_JSON_MUTATION_GUARDS,
    )
    async def cleanup(request: Request) -> CleanupResult:
        return await service_for(request).cleanup_expired()

    return router
