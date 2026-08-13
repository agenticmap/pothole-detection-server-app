"""Pydantic models for the staff auth endpoints (POST /api/v1/auth/*)."""

from pydantic import BaseModel, EmailStr, Field


class LoginRequest(BaseModel):
    """Admin-provisioned staff login (Phase 1 of the staged auth path)."""

    email: EmailStr
    password: str = Field(..., min_length=1, max_length=200)


class RefreshRequest(BaseModel):
    """Exchange a rotating refresh token for a fresh token pair."""

    refresh_token: str = Field(..., min_length=1, max_length=512)


class TokenResponse(BaseModel):
    """Issued token pair. `expires_in` is the access-token lifetime in seconds."""

    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    expires_in: int
