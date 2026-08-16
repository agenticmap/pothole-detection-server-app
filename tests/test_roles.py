"""Unit tests for role-based access control (no DB).

get_current_staff only proves a token is valid, so before require_min_role every
staff route was equally open to a 'viewer'. These tests pin the ranking and, most
importantly, the fail-closed behaviour for unknown/absent roles.
"""

import pytest
from fastapi import Depends, FastAPI, HTTPException
from httpx import ASGITransport, AsyncClient

from app.auth.tokens import StaffPrincipal, create_access_token
from app.dependencies import ROLE_RANK, require_min_role


def _principal(role: str) -> StaffPrincipal:
    return StaffPrincipal(user_id="u1", org_id="org_x", role=role)


async def _check(minimum: str, role: str) -> StaffPrincipal:
    """Invoke the inner dependency directly, bypassing the bearer scheme."""
    dependency = require_min_role(minimum)
    return await dependency(_principal(role))


class TestRanking:
    def test_ranks_are_ordered(self):
        assert ROLE_RANK["viewer"] < ROLE_RANK["staff"] < ROLE_RANK["admin"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("minimum", "role"),
        [
            ("viewer", "viewer"),
            ("viewer", "staff"),
            ("viewer", "admin"),
            ("staff", "staff"),
            ("staff", "admin"),  # admin outranks staff — the allow-list bug this avoids
            ("admin", "admin"),
        ],
    )
    async def test_sufficient_role_passes(self, minimum, role):
        assert (await _check(minimum, role)).role == role

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("minimum", "role"),
        [("staff", "viewer"), ("admin", "viewer"), ("admin", "staff")],
    )
    async def test_insufficient_role_is_403(self, minimum, role):
        with pytest.raises(HTTPException) as exc:
            await _check(minimum, role)
        # 403, not 401: the caller proved who they are, they just aren't allowed.
        assert exc.value.status_code == 403


class TestFailClosed:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("role", ["", "superuser", "Admin", "root"])
    async def test_unknown_or_absent_role_denied(self, role):
        """decode_access_token defaults role to "", so rank 0 must deny."""
        with pytest.raises(HTTPException) as exc:
            await _check("viewer", role)
        assert exc.value.status_code == 403


class TestThroughTheStack:
    """End-to-end through a real bearer header, so the token path is exercised."""

    @pytest.fixture
    def app(self):
        api = FastAPI()

        @api.get("/admin-only")
        async def admin_only(staff=Depends(require_min_role("admin"))):
            return {"role": staff.role}

        return api

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("role", "expected"), [("admin", 200), ("staff", 403), ("viewer", 403)]
    )
    async def test_bearer_token_role_enforced(self, app, role, expected):
        token, _ = create_access_token(user_id="u1", org_id="org_x", role=role)
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.get("/admin-only", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == expected

    @pytest.mark.asyncio
    async def test_missing_token_is_401_not_403(self, app):
        """Unauthenticated must stay 401 — the role check comes after identity."""
        async with AsyncClient(
            transport=ASGITransport(app=app), base_url="http://t"
        ) as ac:
            r = await ac.get("/admin-only")
        assert r.status_code == 401
        assert r.headers.get("WWW-Authenticate") == "Bearer"
