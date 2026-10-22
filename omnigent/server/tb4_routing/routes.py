"""Authenticated, task-free preview of the policy's live candidate set."""

from fastapi import APIRouter, Depends, Request

from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin

from .service import TB4Policy, catalogue, enabled


def create_tb4_router(auth_provider=None):
    router = APIRouter()

    @router.get("/routing/tb4")
    async def status(request: Request):
        require_user(request, auth_provider)
        return {"enabled": enabled()}

    @router.post(
        "/routing/tb4/preview",
        dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
    )
    async def preview(request: Request, policy: TB4Policy):
        require_user(request, auth_provider)
        return await catalogue(policy)

    return router
