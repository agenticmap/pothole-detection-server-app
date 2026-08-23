"""Tests for the city-staff auth tier (Phase 2.4).

Unit tests (password hashing, token mint/verify, JWKS) need no DB. The login /
refresh / detail-gating tests use the db_pool fixture (skips if no Postgres) plus
the httpx client.
"""

import jwt
import pytest

from app.auth.keys import get_jwks
from app.auth.passwords import hash_password, verify_password
from app.auth.tokens import TokenError, create_access_token, decode_access_token

# asyncio_mode=auto (pyproject) runs the async tests; sync unit tests stay sync.

V = {"Accept-Version": "v1"}
BBOX = "-79.40,43.60,-79.30,43.70"
IN_LAT, IN_LON = 43.6532, -79.3832
EMAIL, PASSWORD = "staff@city.gov", "pw-12345678"


# ── Unit: passwords (no DB) ───────────────────────────────────────────────────

def test_password_hash_roundtrip():
    h = hash_password(PASSWORD)
    assert h != PASSWORD
    assert verify_password(PASSWORD, h)
    assert not verify_password("wrong-password", h)


def test_verify_handles_malformed_hash():
    assert verify_password(PASSWORD, "not-a-bcrypt-hash") is False


# ── Unit: tokens (no DB) ──────────────────────────────────────────────────────

def test_token_roundtrip():
    token, ttl = create_access_token(user_id="u1", org_id="org_x", role="staff")
    assert ttl > 0
    principal = decode_access_token(token)
    assert (principal.user_id, principal.org_id, principal.role) == ("u1", "org_x", "staff")


def test_tampered_token_rejected():
    token, _ = create_access_token(user_id="u1", org_id="org_x", role="staff")
    head, payload, sig = token.split(".")
    flipped = payload[:-1] + ("A" if payload[-1] != "A" else "B")
    with pytest.raises(TokenError):
        decode_access_token(f"{head}.{flipped}.{sig}")


def test_wrong_algorithm_token_rejected():
    """A token signed with HS256 must be rejected — decode pins RS256 (alg-confusion guard)."""
    forged = jwt.encode(
        {"sub": "user:u1", "iss": "pothole-detection-server", "org": "org_x", "role": "admin"},
        "attacker-secret",
        algorithm="HS256",
    )
    with pytest.raises(TokenError):
        decode_access_token(forged)


def test_jwks_exposes_rsa_public_key():
    jwks = get_jwks()
    assert jwks["keys"], "JWKS must expose at least one key"
    k = jwks["keys"][0]
    assert k["kty"] == "RSA" and k["alg"] == "RS256" and k["use"] == "sig"
    assert k["kid"] and k["n"] and k["e"]


# ── DB-backed helpers ─────────────────────────────────────────────────────────

async def _seed_staff(conn, *, email=EMAIL, password=PASSWORD, role="staff"):
    await conn.execute(
        "INSERT INTO org (org_id, name) VALUES ('org_test', 'Test City') ON CONFLICT DO NOTHING"
    )
    await conn.execute(
        "INSERT INTO staff_user (user_id, email, password_hash) VALUES ('usr_test', $1, $2) "
        "ON CONFLICT DO NOTHING",
        email,
        hash_password(password),
    )
    await conn.execute(
        "INSERT INTO org_member (org_id, user_id, role) VALUES ('org_test', 'usr_test', $1) "
        "ON CONFLICT DO NOTHING",
        role,
    )


async def _insert_cluster(conn, cluster_id, *, severity=0.7, confidence=0.9, distinct_devices=2):
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, last_seen, source
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            $4, $5, 4, $6, now(), 'crowd'
        )
        """,
        cluster_id, IN_LON, IN_LAT, severity, confidence, distinct_devices,
    )


async def _login(client) -> dict:
    resp = await client.post(
        "/api/v1/auth/login", headers=V, json={"email": EMAIL, "password": PASSWORD}
    )
    assert resp.status_code == 200, resp.text
    return resp.json()


# ── DB-backed: login / refresh ────────────────────────────────────────────────

async def test_login_returns_token_pair(client, db_pool):
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    data = await _login(client)
    assert data["access_token"] and data["refresh_token"]
    assert data["token_type"] == "bearer" and data["expires_in"] > 0


async def test_login_wrong_password_is_401(client, db_pool):
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    resp = await client.post(
        "/api/v1/auth/login", headers=V, json={"email": EMAIL, "password": "WRONG"}
    )
    assert resp.status_code == 401


async def test_login_unknown_email_is_401(client, db_pool):
    resp = await client.post(
        "/api/v1/auth/login", headers=V, json={"email": "ghost@city.gov", "password": PASSWORD}
    )
    assert resp.status_code == 401


async def test_refresh_rotates_and_revokes_old(client, db_pool):
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    rt = (await _login(client))["refresh_token"]

    first = await client.post("/api/v1/auth/refresh", headers=V, json={"refresh_token": rt})
    assert first.status_code == 200
    assert first.json()["refresh_token"] != rt  # rotated

    # Reusing the now-revoked original refresh token must fail.
    reuse = await client.post("/api/v1/auth/refresh", headers=V, json={"refresh_token": rt})
    assert reuse.status_code == 401


async def test_logout_revokes_the_refresh_token(client, db_pool):
    """Regression: logout did not exist, so a signed-out session kept a valid
    refresh token server-side for its full 30-day lifetime."""
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    rt = (await _login(client))["refresh_token"]

    out = await client.post("/api/v1/auth/logout", headers=V, json={"refresh_token": rt})
    assert out.status_code == 204

    # The revoked token can no longer be exchanged.
    resp = await client.post("/api/v1/auth/refresh", headers=V, json={"refresh_token": rt})
    assert resp.status_code == 401


async def test_logout_is_idempotent_and_does_not_probe(client, db_pool):
    """Logout must not reveal whether a token was real — always 204."""
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    rt = (await _login(client))["refresh_token"]

    assert (await client.post(
        "/api/v1/auth/logout", headers=V, json={"refresh_token": rt}
    )).status_code == 204
    # Second time: already revoked.
    assert (await client.post(
        "/api/v1/auth/logout", headers=V, json={"refresh_token": rt}
    )).status_code == 204
    # Never issued at all.
    assert (await client.post(
        "/api/v1/auth/logout", headers=V, json={"refresh_token": "not-a-real-token"}
    )).status_code == 204


async def test_logout_leaves_other_sessions_alone(client, db_pool):
    """Signing out one browser must not sign the account out everywhere."""
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
    first = (await _login(client))["refresh_token"]
    second = (await _login(client))["refresh_token"]
    assert first != second

    assert (await client.post(
        "/api/v1/auth/logout", headers=V, json={"refresh_token": first}
    )).status_code == 204

    # The other session still works.
    resp = await client.post("/api/v1/auth/refresh", headers=V, json={"refresh_token": second})
    assert resp.status_code == 200


# ── DB-backed: detail-endpoint gating ─────────────────────────────────────────

async def test_detail_without_token_is_401(client, db_pool):
    resp = await client.get(f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16", headers=V)
    assert resp.status_code == 401


async def test_detail_with_garbage_token_is_401(client, db_pool):
    headers = {**V, "Authorization": "Bearer not.a.jwt"}
    resp = await client.get(f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16", headers=headers)
    assert resp.status_code == 401


async def test_detail_with_valid_token_returns_full_fields(client, db_pool):
    async with db_pool.acquire() as conn:
        await _seed_staff(conn)
        await _insert_cluster(conn, "clu-1", severity=0.7, confidence=0.9, distinct_devices=2)
    token = (await _login(client))["access_token"]

    headers = {**V, "Authorization": f"Bearer {token}"}
    resp = await client.get(f"/api/v1/potholes/detail?bbox={BBOX}&zoom=16", headers=headers)
    assert resp.status_code == 200
    item = resp.json()["items"][0]
    assert item["id"] == "clu-1"
    assert abs(item["severity"] - 0.7) < 1e-9
    assert abs(item["confidence"] - 0.9) < 1e-9
    assert item["distinct_devices"] == 2 and item["source"] == "crowd"
