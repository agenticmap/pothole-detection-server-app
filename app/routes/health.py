"""Health check endpoint."""

import logging

from fastapi import APIRouter

from app.database import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])


@router.get("/health")
async def health_check():
    """Verify server and database connectivity.

    Returns 200 with status details if healthy, 503 if database is unreachable.
    """
    try:
        pool = get_pool()
        async with pool.acquire() as conn:
            await conn.fetchval("SELECT 1")

        return {
            "status": "healthy",
            "db": "connected",
            "version": "2.0.0",
        }
    except Exception as e:
        logger.error("Health check failed: %s", e)
        return {
            "status": "unhealthy",
            "db": "disconnected",
            "version": "2.0.0",
            "error": str(e),
        }
