"""Staff auth endpoints (Phase 2.4).

  POST /api/v1/auth/login    — email + password → token pair
  POST /api/v1/auth/refresh  — rotating refresh token → fresh token pair
  POST /api/v1/auth/logout   — revoke a refresh token (204, idempotent)
  GET  /.well-known/jwks.json — public keys for token validation (OIDC-swap ready)

These are the ONLY auth-issuing surfaces. Anonymous ingestion + the public
GET /potholes are unaffected.
"""

import logging

from fastapi import APIRouter, HTTPException, Response

from app.auth.keys import get_jwks
from app.auth.service import (
    AuthError,
    authenticate_and_issue,
    refresh_tokens,
    revoke_refresh_token,
)
from app.config import settings
from app.dependencies import ApiVersion, DbPool
from app.models.auth import LoginRequest, RefreshRequest, TokenResponse

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])
well_known_router = APIRouter(tags=["auth"])


def _require_enabled() -> None:
    if not settings.auth_enabled:
        raise HTTPException(status_code=503, detail="Staff auth is disabled.")


@router.post("/login", response_model=TokenResponse)
async def login(body: LoginRequest, pool: DbPool, version: ApiVersion):
    """Authenticate an admin-provisioned staff account."""
    _require_enabled()
    try:
        tokens = await authenticate_and_issue(pool, email=body.email, password=body.password)
    except AuthError:
        # Uniform 401 — never reveal whether the email exists.
        raise HTTPException(status_code=401, detail="Invalid credentials.") from None
    logger.info("Staff login succeeded: email=%s", body.email)
    return TokenResponse(**tokens)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(body: RefreshRequest, pool: DbPool, version: ApiVersion):
    """Rotate a refresh token for a fresh access + refresh pair."""
    _require_enabled()
    try:
        tokens = await refresh_tokens(pool, refresh_token=body.refresh_token)
    except AuthError:
        raise HTTPException(status_code=401, detail="Invalid or expired refresh token.") from None
    return TokenResponse(**tokens)


@router.post("/logout", status_code=204)
async def logout(body: RefreshRequest, pool: DbPool, version: ApiVersion):
    """Revoke a refresh token so a signed-out session cannot be resumed.

    Clearing the client's memory used to be the whole of "logout", which left the
    refresh token valid server-side for its full 30 days. The access token is
    stateless and still valid until it expires (30 min by default) -- revoking
    that would need a denylist, which is a bigger change than this gap warranted.

    Always 204, even for an unknown or already-revoked token: the endpoint must
    not report whether a token was real. POST, not DELETE, because CORS
    allow_methods is GET/POST/OPTIONS.
    """
    _require_enabled()
    revoked = await revoke_refresh_token(pool, refresh_token=body.refresh_token)
    logger.info("Staff logout: refresh token %s", "revoked" if revoked else "not live")
    return Response(status_code=204)


@well_known_router.get("/.well-known/jwks.json")
async def jwks():
    """Publish the public signing key(s). Lets validators (and a future OIDC
    migration) verify tokens by `kid` without sharing the private key."""
    return get_jwks()
