"""Authenticated HTTP surface for the O3 pre-session routing review."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._gzip_route import GZipFileContentRoute
from omnigent.server.routes._origin import require_trusted_origin

from .adviser import ReviewerCaptureError
from .models import (
    CleanupResult,
    ProposalAdjustmentRequest,
    ProposalCreateRequest,
    ProposalDecisionRequest,
    ProposalOutcomeRequest,
    ProposalSessionLinkRequest,
    RoutingProposal,
)
from .omniroute import OmniRouteError
from .registry import SOURCE_POOL_NAME
from .service import O3RoutingReviewService, RoutingReviewError, get_o3_routing_review_service

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
    # Complete catalogue evidence and revisions can span megabytes of JSON.
    router = APIRouter(route_class=GZipFileContentRoute)

    def service_for(request: Request) -> O3RoutingReviewService:
        require_user(request, auth_provider)
        return service_factory()

    @router.get("/o3/routing-review/failed/{review_id}")
    async def failed_review(request: Request, review_id: str) -> object:
        return service_for(request).store.get_failed_review(review_id)

    @router.get("/o3/routing-review/session/{session_id}")
    async def session_reviews(request: Request, session_id: str) -> list[RoutingProposal]:
        return [p for p in service_for(request).store.list() if p.session_id == session_id]

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
    ) -> RoutingProposal | JSONResponse:
        try:
            return await service_for(request).create_proposal(body)
        except ReviewerCaptureError as exc:
            return JSONResponse(
                status_code=502,
                content={
                    "error": {
                        "message": str(exc),
                        "code": "reviewer_parse_failed",
                        "audit_id": exc.review_id,
                    }
                },
            )
        except OmniRouteError as exc:
            raise RoutingReviewError(
                "OmniRoute is temporarily unavailable while preparing the route review; "
                "try again.",
                status_code=503,
                code="omniroute_unavailable",
            ) from exc

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
