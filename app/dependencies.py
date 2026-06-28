"""Shared FastAPI dependencies."""

from typing import Annotated

import asyncpg
from fastapi import Depends, Header, HTTPException

from app.database import get_pool


def get_db_pool() -> asyncpg.Pool:
    """Dependency that provides the database connection pool."""
    return get_pool()


async def require_device_id(
    x_device_id: Annotated[str | None, Header()] = None,
) -> str:
    """Extract and validate the X-Device-Id header.

    Returns the device ID string. Raises 400 if the header is missing or empty.
    """
    if not x_device_id or not x_device_id.strip():
        raise HTTPException(
            status_code=400,
            detail="Missing required header: X-Device-Id",
        )
    return x_device_id.strip()


async def require_version_v1(
    accept_version: Annotated[str | None, Header(alias="Accept-Version")] = None,
) -> str:
    """Validate that Accept-Version header is present and set to 'v1'.

    Returns the version string. Raises 400 if missing or unsupported.
    """
    if not accept_version:
        raise HTTPException(
            status_code=400,
            detail="Missing required header: Accept-Version",
        )
    if accept_version.strip().lower() != "v1":
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported API version: {accept_version}. Only 'v1' is supported.",
        )
    return accept_version.strip()


# Type aliases for cleaner route signatures
DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
DeviceId = Annotated[str, Depends(require_device_id)]
ApiVersion = Annotated[str, Depends(require_version_v1)]
