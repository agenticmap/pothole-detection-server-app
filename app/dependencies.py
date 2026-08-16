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


# Roles are ranked, not an allow-list: 'admin' outranks 'staff' outranks 'viewer'
# (the set mirrors org_member's CHECK constraint in migrations/005_auth.sql). A rank
# comparison can't develop the classic allow-list bug of forgetting to include admin.
ROLE_RANK = {"viewer": 10, "staff": 20, "admin": 30}


def require_min_role(minimum: str):
    """Build a dependency requiring the staff principal to hold at least ``minimum``.

    ``get_current_staff`` only proves the token is valid — every staff route is
    otherwise equally open to a 'viewer'. The role already rides in the JWT
    (app/auth/tokens.py), so this is a claim check, not a second DB round-trip.

    Note the role is a *login-time snapshot*: app/auth/service.py reads org_member.role
    only at login and refresh, so a demotion takes up to one access-token TTL to bite.
    Re-querying here would add a DB round-trip to every tile request; that trade is
    deliberate.

    Returns 403 (authenticated but not permitted), never 401 — the caller proved who
    they are, they just aren't allowed. An unknown or absent role ranks 0 and is
    therefore denied, which matters because decode_access_token defaults role to "".
    """
    floor = ROLE_RANK[minimum]

    async def _dependency(
        staff: Annotated[StaffPrincipal, Depends(get_current_staff)],
    ) -> StaffPrincipal:
        if ROLE_RANK.get(staff.role, 0) < floor:
            raise HTTPException(
                status_code=403,
                detail=f"Requires the '{minimum}' role or higher.",
            )
        return staff

    return _dependency


# Type aliases for cleaner route signatures
DbPool = Annotated[asyncpg.Pool, Depends(get_db_pool)]
DeviceId = Annotated[str, Depends(require_device_id)]
ApiVersion = Annotated[str, Depends(require_version_v1)]
CurrentStaff = Annotated[StaffPrincipal, Depends(get_current_staff)]
# Elevated tiers, used by the Phase 2.5 operator routes.
StaffOrAbove = Annotated[StaffPrincipal, Depends(require_min_role("staff"))]
AdminOnly = Annotated[StaffPrincipal, Depends(require_min_role("admin"))]
