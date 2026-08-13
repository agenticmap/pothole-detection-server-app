"""Staff authentication + token issuance (DB layer).

Login and refresh both end in ``_issue_tokens``: a short-lived RS256 access
token plus a rotating opaque refresh token whose SHA-256 hash is stored so it
can be rotated + revoked. Refresh rotates: the presented token is revoked and a
fresh pair issued, so a stolen-then-used refresh token is single-use.
"""

import hashlib
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta

import asyncpg

from app.auth.passwords import verify_password
from app.auth.tokens import create_access_token
from app.config import settings

logger = logging.getLogger(__name__)


class AuthError(Exception):
    """Any authentication failure (bad credentials, disabled user, bad refresh)."""


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


async def _load_membership(conn: asyncpg.Connection, user_id: str) -> tuple[str, str]:
    """Return (org_id, role) for the user's primary org membership.

    A user can belong to several orgs (the schema allows it); Phase 1 picks the
    earliest membership. Multi-org selection is a future concern.
    """
    row = await conn.fetchrow(
        "SELECT org_id, role FROM org_member WHERE user_id = $1 "
        "ORDER BY created_at, org_id LIMIT 1",
        user_id,
    )
    if row is None:
        raise AuthError("user has no organization membership")
    return row["org_id"], row["role"]


async def _issue_tokens(
    conn: asyncpg.Connection, user_id: str, org_id: str, role: str
) -> dict:
    access_token, expires_in = create_access_token(user_id=user_id, org_id=org_id, role=role)
    refresh_token = secrets.token_urlsafe(48)
    expires_at = datetime.now(UTC) + timedelta(days=settings.auth_refresh_token_ttl_days)
    await conn.execute(
        "INSERT INTO refresh_token (token_id, user_id, token_hash, expires_at) "
        "VALUES ($1, $2, $3, $4)",
        f"rt_{uuid.uuid4().hex}",
        user_id,
        _hash_token(refresh_token),
        expires_at,
    )
    return {
        "access_token": access_token,
        "refresh_token": refresh_token,
        "token_type": "bearer",
        "expires_in": expires_in,
    }


async def authenticate_and_issue(pool: asyncpg.Pool, *, email: str, password: str) -> dict:
    """Verify email+password and return a fresh token pair. Raises AuthError on failure."""
    async with pool.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT user_id, password_hash, disabled FROM staff_user "
            "WHERE lower(email) = lower($1)",
            email,
        )
        # Verify the hash even when the user is missing/disabled would be ideal for
        # timing uniformity; kept simple here — the email index is not a secret.
        if user is None or user["disabled"]:
            raise AuthError("invalid credentials")
        if not verify_password(password, user["password_hash"]):
            raise AuthError("invalid credentials")
        org_id, role = await _load_membership(conn, user["user_id"])
        return await _issue_tokens(conn, user["user_id"], org_id, role)


async def refresh_tokens(pool: asyncpg.Pool, *, refresh_token: str) -> dict:
    """Rotate a refresh token: revoke the presented one, issue a fresh pair."""
    token_hash = _hash_token(refresh_token)
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await conn.fetchrow(
                "SELECT token_id, user_id, expires_at, revoked_at "
                "FROM refresh_token WHERE token_hash = $1 FOR UPDATE",
                token_hash,
            )
            if (
                row is None
                or row["revoked_at"] is not None
                or row["expires_at"] <= datetime.now(UTC)
            ):
                raise AuthError("invalid or expired refresh token")
            await conn.execute(
                "UPDATE refresh_token SET revoked_at = now() WHERE token_id = $1",
                row["token_id"],
            )
            org_id, role = await _load_membership(conn, row["user_id"])
            return await _issue_tokens(conn, row["user_id"], org_id, role)
