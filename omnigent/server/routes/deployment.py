"""Admin-only read-only RTX deployment panel and fixed sync action."""

from __future__ import annotations

import asyncio
import re
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, ConfigDict, field_validator

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import AuthProvider, local_single_user_enabled
from omnigent.server.deployment_controller import (
    DeploymentControllerClient,
    DeploymentControllerError,
    DeploymentControllerUnavailable,
    UnixSocketDeploymentControllerClient,
)
from omnigent.server.routes._auth_helpers import get_user_id
from omnigent.server.routes._content_type import require_json_content_type
from omnigent.server.routes._origin import require_trusted_origin
from omnigent.stores.permission_store import PermissionStore

_OPAQUE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


class SyncO2Request(BaseModel):
    """The only browser-controlled fields accepted by the sync action."""

    model_config = ConfigDict(extra="forbid")

    plan_id: str
    idempotency_key: str

    @field_validator("plan_id")
    @classmethod
    def _valid_plan_id(cls, value: str) -> str:
        if not _OPAQUE_ID.fullmatch(value):
            raise ValueError("invalid deployment plan id")
        return value

    @field_validator("idempotency_key")
    @classmethod
    def _valid_idempotency_key(cls, value: str) -> str:
        try:
            parsed = UUID(value)
        except ValueError as exc:
            raise ValueError("idempotency_key must be a UUID4") from exc
        if parsed.version != 4 or str(parsed) != value:
            raise ValueError("idempotency_key must be a canonical UUID4")
        return value


def _blocked_payload(reason: str) -> dict[str, Any]:
    """Safe UI response when no external controller is installed."""
    return {
        "object": "deployment",
        "status": "blocked",
        "blockers": [reason],
        "can_sync": False,
        "source": None,
        "target": None,
        "expires_at": None,
        "plan_id": None,
    }


async def _require_admin(
    request: Request,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    admin_list: Any,
) -> str:
    """Require an authenticated administrator, including local single-user."""
    if auth_provider is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    user_id = get_user_id(request, auth_provider)
    if user_id is None:
        raise OmnigentError("Authentication required", code=ErrorCode.UNAUTHORIZED)
    if permission_store is None and not (
        local_single_user_enabled() or getattr(auth_provider, "_local_single_user", False)
    ):
        raise OmnigentError(
            "Administrator privileges are not configured for this deployment",
            code=ErrorCode.FORBIDDEN,
        )
    if permission_store is not None:
        is_admin = await asyncio.to_thread(permission_store.is_admin, user_id)
        if not is_admin and not admin_list.is_admin(user_id):
            raise OmnigentError(
                "Admin privileges required to manage deployment",
                code=ErrorCode.FORBIDDEN,
            )
    return user_id


def _controller_error(exc: Exception) -> OmnigentError:
    if isinstance(exc, DeploymentControllerUnavailable):
        return OmnigentError(
            "The external deployment controller is not available.",
            code=ErrorCode.DEPLOYMENT_CONTROLLER_UNAVAILABLE,
        )
    return OmnigentError(
        "The deployment controller rejected the request.",
        code=ErrorCode.CONFLICT,
    )


def create_deployment_router(
    *,
    auth_provider: AuthProvider | None,
    permission_store: PermissionStore | None,
    admin_list: Any,
    controller: DeploymentControllerClient | None = None,
) -> APIRouter:
    """Create the narrow controller-backed deployment router."""
    router = APIRouter()
    client = controller or UnixSocketDeploymentControllerClient()

    @router.get("/deployment")
    async def get_deployment(request: Request) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            response = await asyncio.to_thread(client.request, {"operation": "plan"})
        except DeploymentControllerUnavailable:
            return _blocked_payload("controller_unavailable")
        except DeploymentControllerError as exc:
            raise _controller_error(exc) from exc
        plan = response.get("plan")
        if not isinstance(plan, dict):
            raise _controller_error(DeploymentControllerError("invalid plan"))
        return plan

    @router.post(
        "/deployment/sync-o2",
        status_code=202,
        dependencies=[Depends(require_json_content_type), Depends(require_trusted_origin)],
    )
    async def sync_o2(
        request: Request,
        body: SyncO2Request,
    ) -> dict[str, Any]:
        user_id = await _require_admin(request, auth_provider, permission_store, admin_list)
        try:
            response = await asyncio.to_thread(
                client.request,
                {
                    "operation": "enqueue",
                    "plan_id": body.plan_id,
                    "idempotency_key": body.idempotency_key,
                    # This is resolved by the trusted server auth provider,
                    # never copied from the request body.
                    "requested_by": user_id,
                },
            )
        except DeploymentControllerError as exc:
            raise _controller_error(exc) from exc
        job = response.get("job")
        if not isinstance(job, dict):
            raise _controller_error(DeploymentControllerError("invalid job"))
        return {"object": "deployment_job", "job": job}

    @router.get("/deployment/jobs/{job_id}")
    async def get_deployment_job(request: Request, job_id: str) -> dict[str, Any]:
        await _require_admin(request, auth_provider, permission_store, admin_list)
        if not _OPAQUE_ID.fullmatch(job_id):
            raise OmnigentError("Invalid deployment job id", code=ErrorCode.INVALID_INPUT)
        try:
            response = await asyncio.to_thread(
                client.request,
                {"operation": "job", "job_id": job_id},
            )
        except DeploymentControllerError as exc:
            raise _controller_error(exc) from exc
        job = response.get("job")
        if not isinstance(job, dict):
            raise _controller_error(DeploymentControllerError("invalid job"))
        return {"object": "deployment_job", "job": job}

    return router
