-- ============================================================================
-- Pothole Detection Platform — Initial Schema (Phase 2.0)
-- ============================================================================
-- This migration establishes the core tables for the ingestion server.
--
-- Naming convention follows enterprise-architecture-plan.md §5.2:
--   - asset_observation  (not "event" — avoids collision with analytics/telemetry)
--   - asset_frame        (not "frame" — namespaced under asset_*)
--   - asset_cluster      (not "pothole_cluster" — generic from day one)
--
-- The mobile v1 wire contract endpoints (/api/v1/events, /api/v1/frames) are
-- preserved as-is — internal table naming is the server's concern.
-- ============================================================================

-- Enable PostGIS extension for geographic operations
CREATE EXTENSION IF NOT EXISTS postgis;

-- ============================================================================
-- ASSET TYPE REGISTRY
-- ============================================================================
-- Lookup table mapping asset_type → metadata. New asset types in Year 2+
-- require only a new row here + a detection model. No schema change needed.
-- ============================================================================

CREATE TABLE IF NOT EXISTS asset_type_registry (
    asset_type      TEXT PRIMARY KEY,
    display_name    TEXT NOT NULL,
    description     TEXT,
    icon_url        TEXT,
    created_at      TIMESTAMPTZ DEFAULT now()
);

-- Seed the initial asset type
INSERT INTO asset_type_registry (asset_type, display_name, description)
VALUES ('pothole', 'Pothole', 'Road surface damage detected by accelerometer or camera.')
ON CONFLICT (asset_type) DO NOTHING;

-- ============================================================================
-- ASSET OBSERVATION (sensor events from mobile devices)
-- ============================================================================
-- One row per detection event from the sensor pipeline.
-- client_id is the PK (UUID generated on-device before Room insert).
-- Idempotent: duplicate uploads are handled via ON CONFLICT DO NOTHING.
-- ============================================================================

CREATE TABLE IF NOT EXISTS asset_observation (
    client_id           TEXT PRIMARY KEY,
    device_id           TEXT NOT NULL,
    asset_type          TEXT NOT NULL DEFAULT 'pothole',
    schema_version      INT NOT NULL DEFAULT 1,
    ts_utc              TIMESTAMPTZ NOT NULL,
    geom                GEOGRAPHY(POINT, 4326) NOT NULL,
    speed_mps           DOUBLE PRECISION,
    bearing_deg         DOUBLE PRECISION,
    speed_accuracy_mps  DOUBLE PRECISION,
    accel_max_g         DOUBLE PRECISION,
    accel_std           DOUBLE PRECISION,
    magnitude           DOUBLE PRECISION,
    gbar_in_max         DOUBLE PRECISION,
    time_in_max         DOUBLE PRECISION,
    time_in_min         DOUBLE PRECISION,
    confidence          DOUBLE PRECISION NOT NULL DEFAULT 1.0,
    raw_window_b64      TEXT,
    visual_confirmed    BOOLEAN,
    frame_client_id     TEXT,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Spatial index for bbox queries (GET /potholes?bbox=...)
CREATE INDEX IF NOT EXISTS idx_asset_observation_geom
    ON asset_observation USING GIST(geom);

-- Device lookup for rate limiting and per-device queries
CREATE INDEX IF NOT EXISTS idx_asset_observation_device_id
    ON asset_observation (device_id);

-- Temporal index for "since" queries
CREATE INDEX IF NOT EXISTS idx_asset_observation_received_at
    ON asset_observation (received_at);

-- ============================================================================
-- ASSET FRAME (camera frames from mobile devices)
-- ============================================================================
-- One row per camera frame. Linked to observations via event_client_id.
-- JPEG binary is stored in object storage; jpeg_url is the reference.
-- ============================================================================

CREATE TABLE IF NOT EXISTS asset_frame (
    client_id           TEXT PRIMARY KEY,
    device_id           TEXT NOT NULL,
    event_client_id     TEXT,
    ts_utc              TIMESTAMPTZ NOT NULL,
    geom                GEOGRAPHY(POINT, 4326) NOT NULL,
    device_probability  DOUBLE PRECISION,
    device_model_id     TEXT,
    device_detections   JSONB,
    jpeg_url            TEXT NOT NULL,
    server_probability  DOUBLE PRECISION,
    server_model_id     TEXT,
    server_detections   JSONB,
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    processed_at        TIMESTAMPTZ
);

-- Spatial index for bbox queries
CREATE INDEX IF NOT EXISTS idx_asset_frame_geom
    ON asset_frame USING GIST(geom);

-- Device lookup
CREATE INDEX IF NOT EXISTS idx_asset_frame_device_id
    ON asset_frame (device_id);

-- Unprocessed frames index for the fusion engine queue
CREATE INDEX IF NOT EXISTS idx_asset_frame_unprocessed
    ON asset_frame (received_at) WHERE processed_at IS NULL;

-- ============================================================================
-- ASSET CLUSTER (aggregated detections)
-- ============================================================================
-- Clusters are generated by the server-side ST_ClusterDBSCAN job (Phase 2.2).
-- Read-only from the mobile app's perspective.
-- ============================================================================

CREATE TABLE IF NOT EXISTS asset_cluster (
    cluster_id          TEXT PRIMARY KEY,
    asset_type          TEXT NOT NULL DEFAULT 'pothole',
    centroid            GEOGRAPHY(POINT, 4326) NOT NULL,
    severity            DOUBLE PRECISION,
    confidence          DOUBLE PRECISION,
    observation_count   INT NOT NULL DEFAULT 0,
    distinct_devices    INT NOT NULL DEFAULT 0,
    last_seen           TIMESTAMPTZ,
    source              TEXT CHECK (source IN ('crowd', 'verified', 'ml')),
    repaired_at         TIMESTAMPTZ,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Spatial index for tile generation
CREATE INDEX IF NOT EXISTS idx_asset_cluster_centroid
    ON asset_cluster USING GIST(centroid);

-- Filter by asset type
CREATE INDEX IF NOT EXISTS idx_asset_cluster_type
    ON asset_cluster (asset_type);

-- ============================================================================
-- OBSERVATION-CLUSTER LINK (many-to-many)
-- ============================================================================
-- Links observations and frames to their parent cluster.
-- ============================================================================

CREATE TABLE IF NOT EXISTS observation_cluster_link (
    cluster_id          TEXT NOT NULL REFERENCES asset_cluster(cluster_id) ON DELETE CASCADE,
    member_id           TEXT NOT NULL,
    kind                TEXT NOT NULL CHECK (kind IN ('observation', 'frame')),
    fused_confidence    DOUBLE PRECISION,
    PRIMARY KEY (cluster_id, member_id, kind)
);

-- ============================================================================
-- FUSION RUN (audit trail for fusion engine executions)
-- ============================================================================
-- Every time the fusion engine processes a batch, a run record is created.
-- This enables A/B comparison across engine versions and full reproducibility.
-- ============================================================================

CREATE TABLE IF NOT EXISTS fusion_run (
    run_id              TEXT PRIMARY KEY,
    engine_version      TEXT NOT NULL,
    weights_jsonb       JSONB,
    started_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at        TIMESTAMPTZ,
    inputs_count        INT,
    outputs_count       INT,
    metrics_jsonb       JSONB
);

-- ============================================================================
-- FUSION PAIR (observation↔frame pairing with fused confidence)
-- ============================================================================
-- Links a sensor observation to a camera frame with temporal/spatial proximity.
-- Produced by the fusion engine; consumed by the clustering job.
-- ============================================================================

CREATE TABLE IF NOT EXISTS fusion_pair (
    event_client_id     TEXT NOT NULL REFERENCES asset_observation(client_id) ON DELETE CASCADE,
    frame_client_id     TEXT NOT NULL REFERENCES asset_frame(client_id) ON DELETE CASCADE,
    fused_confidence    DOUBLE PRECISION,
    delta_ms            BIGINT,
    delta_m             DOUBLE PRECISION,
    fusion_run_id       TEXT REFERENCES fusion_run(run_id) ON DELETE SET NULL,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_client_id, frame_client_id)
);

-- ============================================================================
-- DEVICE RATE LIMIT (persistent rate tracking — future use)
-- ============================================================================
-- Placeholder for Redis-less rate limiting. The in-memory rate limiter
-- suffices for single-instance; this table supports multi-instance deployments.
-- ============================================================================

CREATE TABLE IF NOT EXISTS device_rate_limit (
    device_id           TEXT NOT NULL,
    resource            TEXT NOT NULL,  -- 'events' | 'frames'
    window_start        TIMESTAMPTZ NOT NULL,
    request_count       INT NOT NULL DEFAULT 0,
    PRIMARY KEY (device_id, resource, window_start)
);
