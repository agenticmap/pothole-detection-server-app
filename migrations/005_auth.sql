-- ============================================================================
-- Pothole Detection Platform — City-staff auth + RBAC (Phase 2.4)
-- ============================================================================
-- Additive, idempotent migration. Does NOT modify earlier migrations. Applied
-- after them by the sorted-glob loader in app/database.py::run_migrations.
--
-- Introduces a SECOND identity tier. The anonymous device-ID-only tier
-- (asset_observation / asset_frame ingestion, public GET /potholes) is
-- unchanged and stays account-free. These tables back ONLY the elevated
-- "city staff" tier that unlocks the detailed read path (GET /potholes/detail).
--
-- Model (research-backed, see docs/phase-2.4-auth-plan):
--   org           — a municipality / road authority (tenant).
--   staff_user    — a global staff identity (email + bcrypt password).
--   org_member    — join: which user has which role in which org. Role lives on
--                   the membership, NOT the user, so one person can hold
--                   different roles across municipalities without bleed.
--   refresh_token — server-side store of issued refresh tokens (hash only) so
--                   they can be rotated + revoked.
--
-- Per-municipality row-level data scoping (RLS keyed on org) is DEFERRED; the
-- org_id columns are laid down now so it can be added without a schema change.
-- ============================================================================

-- ── org (tenant) ────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS org (
    org_id      TEXT PRIMARY KEY,             -- e.g. 'org_cambridge'
    name        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ── staff_user (global identity) ──────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS staff_user (
    user_id        TEXT PRIMARY KEY,          -- e.g. 'usr_<uuid>'
    email          TEXT NOT NULL,
    password_hash  TEXT NOT NULL,             -- bcrypt
    full_name      TEXT,
    disabled       BOOLEAN NOT NULL DEFAULT FALSE,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Case-insensitive unique email (login is by email; avoid dup accounts by case).
CREATE UNIQUE INDEX IF NOT EXISTS idx_staff_user_email_lower
    ON staff_user (lower(email));

-- ── org_member (RBAC: role scoped per-org) ────────────────────────────────────
CREATE TABLE IF NOT EXISTS org_member (
    org_id      TEXT NOT NULL REFERENCES org(org_id) ON DELETE CASCADE,
    user_id     TEXT NOT NULL REFERENCES staff_user(user_id) ON DELETE CASCADE,
    role        TEXT NOT NULL DEFAULT 'staff'
                CHECK (role IN ('admin', 'staff', 'viewer')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (org_id, user_id)
);

CREATE INDEX IF NOT EXISTS idx_org_member_user ON org_member (user_id);

-- ── refresh_token (rotation + revocation) ─────────────────────────────────────
-- Opaque refresh tokens are stored as a SHA-256 hash (never plaintext). On
-- refresh: look up by hash, check not-revoked + not-expired, then revoke and
-- issue a new one (rotation). revoked_at also lets an admin kill a session.
CREATE TABLE IF NOT EXISTS refresh_token (
    token_id    TEXT PRIMARY KEY,             -- jti
    user_id     TEXT NOT NULL REFERENCES staff_user(user_id) ON DELETE CASCADE,
    token_hash  TEXT NOT NULL,                -- sha256(opaque token)
    issued_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at  TIMESTAMPTZ NOT NULL,
    revoked_at  TIMESTAMPTZ
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_refresh_token_hash ON refresh_token (token_hash);
CREATE INDEX IF NOT EXISTS idx_refresh_token_user ON refresh_token (user_id);
