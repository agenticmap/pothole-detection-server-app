"""Mint + verify RS256 JWT access tokens for the staff tier.

The claim shape is deliberately small and issuer-agnostic so the issuer can be
swapped for OIDC later without the API caring:
    sub  — "user:<user_id>" (unique string subject)
    iss  — settings.auth_jwt_issuer (validated on decode)
    iat/exp — issued-at / short expiry
    org  — the staff member's organization id
    role — 'admin' | 'staff' | 'viewer' (RBAC; role, not a permission list)
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import jwt

from app.auth.keys import get_key_material
from app.config import settings

ALGORITHM = "RS256"


class TokenError(Exception):
    """Raised when an access token is missing, malformed, expired, or untrusted."""


@dataclass(frozen=True)
class StaffPrincipal:
    """The authenticated staff identity decoded from a valid access token."""

    user_id: str
    org_id: str
    role: str


def create_access_token(*, user_id: str, org_id: str, role: str) -> tuple[str, int]:
    """Return (signed_jwt, expires_in_seconds)."""
    km = get_key_material()
    now = datetime.now(UTC)
    ttl_seconds = settings.auth_access_token_ttl_minutes * 60
    payload = {
        "sub": f"user:{user_id}",
        "iss": settings.auth_jwt_issuer,
        "iat": now,
        "exp": now + timedelta(seconds=ttl_seconds),
        "org": org_id,
        "role": role,
    }
    token = jwt.encode(payload, km.private_pem, algorithm=ALGORITHM, headers={"kid": km.kid})
    return token, ttl_seconds


def decode_access_token(token: str) -> StaffPrincipal:
    """Verify signature + claims and return the principal. Raises TokenError on failure."""
    km = get_key_material()
    try:
        payload = jwt.decode(
            token,
            km.public_pem,
            algorithms=[ALGORITHM],  # pinned — blocks alg-confusion (e.g. alg=none/HS256)
            issuer=settings.auth_jwt_issuer,
            options={"require": ["exp", "iat", "sub", "iss"]},
        )
    except jwt.PyJWTError as e:
        raise TokenError(str(e)) from e

    sub = payload.get("sub", "")
    user_id = sub.split("user:", 1)[1] if sub.startswith("user:") else sub
    if not user_id:
        raise TokenError("token missing subject")
    return StaffPrincipal(
        user_id=user_id,
        org_id=payload.get("org", ""),
        role=payload.get("role", ""),
    )
