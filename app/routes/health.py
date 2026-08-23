"""Health check endpoint."""

import logging

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from app.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

_VERSION = "2.0.0"


@router.get("/health")
async def health_check():
    """Verify server and database connectivity.

    Returns 200 with status details if healthy, 503 if the database is unreachable.

    The 503 is load-bearing: this used to `return` the unhealthy body, which FastAPI
    served as HTTP 200, so any status-code-only uptime check (including the compose
    healthcheck) reported green against a dead database. The body shape is unchanged
    so consumers that parse `status` still work.
    """
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

        return {
            "status": "healthy",
            "db": "connected",
            "version": _VERSION,
        }
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return JSONResponse(
            status_code=503,
            content={
                "status": "unhealthy",
                "db": "disconnected",
                "version": _VERSION,
                "error": str(e),
            },
        )
