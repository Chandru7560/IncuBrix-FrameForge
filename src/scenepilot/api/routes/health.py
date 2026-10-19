"""Service liveness and dependency readiness routes."""

from __future__ import annotations

import httpx
from fastapi import APIRouter, Request
from sqlalchemy import text

from scenepilot.api.schemas import ErrorResponse

router = APIRouter(tags=["operations"])


@router.get("/health", operation_id="health_check", summary="Check service liveness")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get(
    "/ready",
    operation_id="readiness_check",
    summary="Check service dependencies",
    responses={503: {"model": ErrorResponse, "description": "A required dependency is not ready."}},
)
async def ready(request: Request) -> dict[str, str]:
    try:
        with request.app.state.session_factory() as session:
            session.execute(text("SELECT 1"))
    except Exception as exc:
        from scenepilot.errors import ApiError

        raise ApiError(
            503,
            "DEPENDENCY_NOT_READY",
            "The database is not ready.",
            retryable=True,
            details={"dependency": "database"},
        ) from exc

    storage_url = request.app.state.settings.storage_health_url
    if storage_url:
        try:
            async with httpx.AsyncClient(
                timeout=request.app.state.settings.url_fetch_timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.get(storage_url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            from scenepilot.errors import ApiError

            raise ApiError(
                503,
                "DEPENDENCY_NOT_READY",
                "Object storage is not ready.",
                retryable=True,
                details={"dependency": "object_storage"},
            ) from exc
    return {"status": "ready"}
