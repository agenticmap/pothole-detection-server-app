"""Shared FastAPI dependencies."""

from typing import Annotated

import asyncpg
from fastapi import Depends, Header, HTTPException
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from app.auth.tokens import StaffPrincipal, TokenError, decode_access_token
from app.database import get_pool
from app.validators import is_safe_id


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
    device_id = x_device_id.strip()
    if not is_safe_id(device_id):
        raise HTTPException(
            status_code=400,
            detail="X-Device-Id must be 1-64 chars of [A-Za-z0-9._-].",
        )
    return device_id


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


# ── Staff auth (Phase 2.4) ────────────────────────────────────────────────────
# auto_error=False so we can return a 401 with a WWW-Authenticate header rather
# than FastAPI's default 403, and so this scheme never affects anonymous routes.
_bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_staff(
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer_scheme)] = None,
) -> StaffPrincipal:
    """Require a valid staff access token. Raises 401 otherwise.

    Apply ONLY to staff routes (e.g. /potholes/detail). Routes that omit this
    dependency stay anonymous — the device-ID-only contract is untouched.
    """
    if credentials is None or not credentials.credentials:
        raise HTTPException(
            status_code=401,
            detail="Missing bearer token.",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        return decode_access_token(credentials.credentials)
    except TokenError as e:
        raise HTTPException(
            status_code=401,
            detail="Invalid or expired token.",
            headers={"WWW-Authenticate": "Bearer"},
        ) from e


# Type aliases for cleaner route signatures
DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
DeviceId = Annotated[str, Depends(require_device_id)]
ApiVersion = Annotated[str, Depends(require_version_v1)]
CurrentStaff = Annotated[StaffPrincipal, Depends(get_current_staff)]
