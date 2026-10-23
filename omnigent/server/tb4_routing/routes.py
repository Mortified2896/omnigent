"""Authenticated, task-free preview of the policy's live candidate set."""

import asyncio

from fastapi import APIRouter, Depends, Request

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.routes._auth_helpers import require_user
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin

from .availability import ProbePolicy, gateway_request
from .service import TB4Policy, catalogue, enabled


def create_tb4_router(auth_provider=None, permission_store=None):
    router = APIRouter()

    async def operator(request):
        user = require_user(request, auth_provider)
        if permission_store is not None and (
            user is None or not await asyncio.to_thread(permission_store.is_admin, user)
        ):
            raise OmnigentError(
                "Model availability is available to administrators", code=ErrorCode.FORBIDDEN
            )
        if not enabled():
            raise OmnigentError("Model availability is not enabled", code=ErrorCode.NOT_FOUND)
        return user

    @router.get("/routing/availability")
    async def overview(request: Request):
        await operator(request)
        return await gateway_request("GET")

    guards = [Depends(require_json_content_type), Depends(require_trusted_origin)]

    @router.post("/routing/availability/refresh", dependencies=guards)
    async def refresh(request: Request):
        await operator(request)
        return await gateway_request("POST", "/refresh", {})

    @router.post("/routing/availability/plan", dependencies=guards)
    async def test_plan(request: Request, policy: ProbePolicy):
        await operator(request)
        return await gateway_request("POST", "/plan", policy.model_dump())

    @router.post("/routing/availability/test", dependencies=guards)
    async def test_models(request: Request, policy: ProbePolicy):
        await operator(request)
        return await gateway_request("POST", "/test", policy.model_dump())

    @router.post("/routing/availability/cancel", dependencies=guards)
    async def cancel_test(request: Request):
        await operator(request)
        return await gateway_request("POST", "/cancel", {})

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
