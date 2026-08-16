---
updated: 2026-08-16
---

# Phase 2.4 — City-staff auth tier (server side)

> Status: **Implemented** (server side). App side in progress in the mobile repo.

## Why

The public read path (`GET /api/v1/potholes`) returned full per-cluster detail —
`severity`, `confidence`, `distinct_devices`, `last_seen`, `source` — to anyone.
That detail is the municipal/MTO product (severity-based repair prioritization), so
it must be gated to authorized **city staff**, while the anonymous crowd tier stays
account-free.

This phase introduces a **second identity tier**. The device-ID-only anonymous tier
(ingestion + public read) is unchanged.

## What landed

- **Dependencies** (`requirements.txt`, `pyproject.toml`) — `PyJWT[crypto]` (RS256 needs the
  crypto extra) and `bcrypt`. PyJWT, not python-jose (the latter is abandoned, last release
  2021). `requirements.txt` pins `bcrypt==4.2.1`; tested locally against 5.0.0 (API-identical).
- **`migrations/005_auth.sql`** — `org`, `staff_user` (bcrypt), `org_member`
  (RBAC: role scoped per-org, `CHECK IN ('admin','staff','viewer')`), `refresh_token`
  (hash-stored, rotation + revocation). `org_id` columns laid down so per-municipality
  RLS can be added later without a schema change.
- **`app/auth/`** —
  - `keys.py` — RS256 keypair load (PEM via config) or ephemeral dev keypair; JWKS builder.
  - `tokens.py` — mint/verify RS256 access tokens. Claims: `sub`, `iss`, `iat`, `exp`,
    `org`, `role`. Decode pins `algorithms=["RS256"]` and validates `iss` (alg-confusion guard).
  - `passwords.py` — bcrypt hash/verify (72-byte guard).
  - `service.py` — `authenticate_and_issue`, `refresh_tokens` (rotates: revoke presented +
    issue fresh pair).
- **`app/dependencies.py`** — `get_current_staff` (HTTPBearer, 401 + `WWW-Authenticate`)
  and the `CurrentStaff` type alias. Applied **only** to staff routes.
- **`app/routes/auth.py`** — `POST /api/v1/auth/login`, `POST /api/v1/auth/refresh`,
  `GET /.well-known/jwks.json`.
- **Read path split** (`routes/potholes.py`, `services/cluster_query_service.py`,
  `models/potholes.py`):
  - `GET /api/v1/potholes` (public) → **locations only** (`PublicPotholeItem` /
    `PublicClusterItem`: id/lat/lon, or centroid/count). No severity/confidence leak.
  - `GET /api/v1/potholes/detail` (staff, `CurrentStaff`-guarded) → full `PotholeItem` /
    `ClusterAggItem`.

## Staged auth (why RS256, not HS256)

Phase 1 issues tokens from an in-house admin-provisioned account store. RS256 + a
published JWKS means relying parties validate by `kid` against the public key, so the
token **issuer can later be swapped for OIDC/SSO** (Google Workspace / Entra / SAML)
without changing the API's validation path or the app. Phase 2 (OIDC) is deferred until a
municipality requires their own IdP.

## Config (`app/config.py`)

`auth_enabled`, `auth_jwt_private_key_pem` (+ optional `_public_key_pem`), `auth_jwt_kid`,
`auth_jwt_issuer`, `auth_access_token_ttl_minutes` (30), `auth_refresh_token_ttl_days` (30).
Empty private key in `development` → ephemeral keypair (warns; tokens don't survive restart).
Empty private key outside development → hard error (fail closed).

## Provisioning a staff account

> **Superseded by Phase 2.5.** Use `scripts/create_staff.py` — it validates the email with the
> same `EmailStr` the login route uses (otherwise you can create an account that can never log
> in) and takes the password from `POTHOLE_STAFF_PASSWORD` or a prompt rather than `argv`.
>
> ```bash
> POTHOLE_STAFF_PASSWORD='…' python scripts/create_staff.py \
>     --org org_cambridge --name "City of Cambridge" \
>     --email jane@cambridge.gov --role staff
> ```

The original manual recipe, kept for reference:

```sql
INSERT INTO org (org_id, name) VALUES ('org_cambridge', 'City of Cambridge');
-- password_hash: app.auth.passwords.hash_password('...')
INSERT INTO staff_user (user_id, email, password_hash)
    VALUES ('usr_jane', 'jane@cambridge.gov', '<bcrypt>');
INSERT INTO org_member (org_id, user_id, role) VALUES ('org_cambridge', 'usr_jane', 'staff');
```

## Tests

`tests/test_auth.py` — password roundtrip, token mint/verify, tampered + wrong-alg
rejection, JWKS shape (no DB); login (+ wrong-password / unknown-email 401), refresh
rotation + reuse-revocation, detail-endpoint gating (401 without/garbage token, full
fields with a valid token), all DB-backed. `tests/test_potholes.py` updated: public tier
asserts detail fields are **absent**. Full suite: 86 passed.

## Out of scope (later)
- OIDC/SSO issuer swap (Phase 2 of the staged path).
- Per-municipality RLS data scoping (columns ready; policies not written). **Now visible:**
  `asset_cluster` has no `org_id`, so the Phase 2.5 repair endpoint lets any staff member of
  any org mutate any city's clusters.
- ~~Repair-management~~ **shipped in Phase 2.5** (`POST /api/v1/clusters/{id}/repair`).
  Export / raw-data staff entitlements are still unbuilt.
- Timing-uniform login (verify hash even for missing users).

## Follow-ups landed in Phase 2.5

`get_current_staff` proved a token was valid but **no route ever consulted `staff.role`** —
every staff route was equally open to a `viewer`. Phase 2.5 added `require_min_role` (ranked,
fail-closed) and, for write endpoints, `require_min_role_live`, which re-reads
`org_member.role` because the JWT's role is a login-time snapshot that stays stale for up to
`auth_access_token_ttl_minutes`. See [`phase-2.5-dashboard-plan.md`](./phase-2.5-dashboard-plan.md).
