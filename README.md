---
updated: 2026-08-23
---

# Pothole Detection — Ingestion Server

**Phase 2.0 Backend** — Receives sensor events and camera frames from mobile devices, validates payloads, and stores them in PostgreSQL + PostGIS.

| Component | Technology |
|---|---|
| Language | Python 3.12 |
| Framework | FastAPI 0.115+ |
| Database | PostgreSQL 16 + PostGIS 3.4 (Supabase in production) |
| DB Driver | asyncpg (async, connection pooled) |
| Validation | Pydantic v2 |
| Storage | Local filesystem (dev) / Supabase Storage (prod) |
| Container | Docker + docker-compose |

---

## Quick Start

### Prerequisites

- Python 3.12+
- Docker & Docker Compose (for local PostgreSQL + PostGIS)

### 1. Start the Database

```bash
cd server
docker compose up -d
```

This launches PostgreSQL 16 with PostGIS on **`localhost:5433`** (compose maps host 5433 -> container 5432; the service is named `postgres`).

Compose also defines an **`app`** service — the API on `:8000`, with the operator dashboard built
into the image and a healthcheck. So `docker compose up -d` is the whole stack, and steps 2–4
below are the *alternative* to it for local development, not a follow-on. Running both at once
fails with "port is already allocated".

```bash
docker compose up -d            # database + API + dashboard
docker compose up -d postgres   # database only, then run uvicorn by hand (steps 2-4)
```

Migrations are applied by the server at startup and tracked in `schema_migrations`, so each file
runs at most once, in **every** environment. (`migrations/` is additionally mounted at
`docker-entrypoint-initdb.d` for the container's own first boot.)

### 2. Create Python Environment

```bash
cd server
python -m venv .venv

# Windows
.venv\Scripts\activate

# macOS/Linux
source .venv/bin/activate

pip install -r requirements.txt
```

### 3. Configure Environment

```bash
cp .env.example .env
# Edit .env if needed — defaults work for local Docker development
```

### 4. Run the Server

```bash
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

The server starts at `http://localhost:8000`. Interactive API docs are at `http://localhost:8000/docs`.

### 5. Verify

```bash
# Health check
curl http://localhost:8000/health

# Expected response:
# {"status":"healthy","db":"connected","version":"2.0.0"}
#
# With the database down this returns HTTP 503 and {"status":"unhealthy",...},
# so `curl -f` / an uptime check will correctly fail.
```

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `DATABASE_URL` | `postgresql://pothole:pothole@localhost:5433/pothole_db` | PostgreSQL connection string (note port **5433**) |
| `DATABASE_USE_POOLER` | `false` | Set `true` for Supabase connection pooler (disables statement cache) |
| `DATABASE_MIN_CONNECTIONS` | `5` | asyncpg pool minimum size |
| `DATABASE_MAX_CONNECTIONS` | `20` | asyncpg pool maximum size |
| `STORAGE_BACKEND` | `local` | Frame JPEG storage: `local` or `supabase` |
| `STORAGE_LOCAL_PATH` | `./storage/frames` | Local storage directory |
| `SUPABASE_URL` | _(empty)_ | Supabase project URL (for storage) |
| `SUPABASE_SERVICE_KEY` | _(empty)_ | Supabase service-role key |
| `SUPABASE_STORAGE_BUCKET` | `frames` | Storage bucket name |
| `CORS_ORIGINS` | `http://localhost:3000,http://localhost:5173` | Allowed CORS origins |
| `ENV` | `development` | Environment: `development` / `staging` / `production` |
| `RATE_LIMIT_EVENTS_PER_HOUR` | `5000` | Max events per device per hour |
| `RATE_LIMIT_FRAMES_PER_HOUR` | `5000` | Max frames per device per hour |
| `MAX_BATCH_SIZE` | `100` | Max events in a single POST batch |
| `MAX_FRAME_SIZE_BYTES` | `10485760` | Max JPEG size (10 MB) |
| `DETECTION_ENABLED` | `false` | Server-side detection. Off until a model exists — [runbook](./docs/phase-2.7-runbook.md), [as-built](./docs/phase-2.7-detection-enablement.md) |
| `DETECTION_BACKEND` | `none` | `none` / `onnx` / `http` / `hybrid` |
| `DETECTION_MODEL_PATH` | _(empty)_ | Path to a **raw** Ultralytics ONNX export (`opset=12 nms=False`); lives in `models/` |
| `DETECTION_ROI_ENABLED` | `true` | Crop to the road band before inference — uploads are portrait windshield frames that are mostly sky |

Every detection and VLM knob is documented in `.env.example`.

---

## API Endpoint Contracts

### `GET /health`

Health check endpoint. Acquires a pooled connection and runs `SELECT 1`.

**Response 200:**
```json
{
  "status": "healthy",
  "db": "connected",
  "version": "2.0.0"
}
```

**Response 503** (database unreachable):
```json
{
  "status": "unhealthy",
  "db": "disconnected",
  "version": "2.0.0",
  "error": "..."
}
```

> This used to return **200** with the `unhealthy` body, so a status-code-only uptime check
> reported green against a dead database. It is now a real 503; the body shape is unchanged.
> The compose healthcheck depends on this.

---

### `POST /api/v1/events`

Batch sensor event ingestion.

**Required Headers:**
| Header | Value | Description |
|---|---|---|
| `X-Device-Id` | UUID string | Device identifier |
| `Accept-Version` | `v1` | API version (frozen) |
| `Content-Type` | `application/json` | — |

**Request Body:**
```json
{
  "events": [
    {
      "client_id": "550e8400-e29b-41d4-a716-446655440000",
      "schema_version": 1,
      "ts": "2026-05-27T10:30:00Z",
      "lat": 43.6532,
      "lon": -79.3832,
      "speed_mps": 12.5,
      "bearing_deg": 180.0,
      "speed_accuracy_mps": 1.2,
      "accel_max_g": 2.3,
      "accel_std": 0.8,
      "magnitude": 3.1,
      "gbar_in_max": 1.5,
      "time_in_max": 0.05,
      "time_in_min": 0.02,
      "confidence": 0.95,
      "raw_window_b64": "H4sIAAAAAAAA...",
      "visual_confirmed": true,
      "frame_client_id": "660e8400-e29b-41d4-a716-446655440000"
    }
  ]
}
```

**Field Validation Rules:**

> **`app/models/__init__.py` is the source of truth.** This table is a convenience copy and has
> drifted before — it previously understated the accel/magnitude ceilings by 4×. Check the model
> (or `/openapi.json` on a running instance) before treating any number here as the contract.

| Field | Type | Required | Constraints |
|---|---|---|---|
| `client_id` | string | yes | 1–64 chars (format not enforced) |
| `schema_version` | int | no | Default 1, range [1, 100] |
| `ts` | string | yes | ISO-8601 UTC with timezone |
| `lat` | float | yes | [-90, 90] |
| `lon` | float | yes | [-180, 180] |
| `speed_mps` | float | yes | [0, 200] |
| `bearing_deg` | float | yes | [0, 360] |
| `speed_accuracy_mps` | float | no | [0, 200] |
| `accuracy_m` | float | no | [0, 10 000] |
| `accel_max_g` | float | yes | [-200, 200] |
| `accel_std` | float | yes | [0, 200] |
| `magnitude` | float | yes | [0, 2000] |
| `gbar_in_max` | float | no | [0, 2000] |
| `time_in_max` | float | no | ≥ 0 |
| `time_in_min` | float | no | ≥ 0 |
| `confidence` | float | yes | [0, 1] |
| `raw_window_b64` | string | no | Max 50,000 chars |
| `visual_confirmed` | bool | no | — |
| `frame_client_id` | string | no | Max 64 chars |

Unknown fields are **ignored**, not rejected (Pydantic v2 default `extra="ignore"`), so the client
may add fields ahead of the server. A single out-of-range field **422s the entire batch** — the
per-row tolerance described elsewhere applies to DB errors, not validation.

**Response 200:**
```json
{
  "accepted": [
    "550e8400-e29b-41d4-a716-446655440000"
  ],
  "rejected": [
    {
      "client_id": "bad-event-id",
      "reason": "Database constraint violation"
    }
  ]
}
```

**Idempotency:** Re-uploading the same `client_id` returns it in `accepted` without creating a duplicate row.

**Error Responses:**
| Status | Condition |
|---|---|
| 400 | Missing `X-Device-Id` or `Accept-Version` header |
| 422 | Payload validation failure (invalid fields, empty batch, etc.) |
| 429 | Rate limit exceeded (> 5000 events/hour per device) |
| 500 | Internal server error |

---

### `POST /api/v1/frames`

Single camera frame upload (multipart).

**Required Headers:**
| Header | Value |
|---|---|
| `X-Device-Id` | UUID string |
| `Accept-Version` | `v1` |

**Request Body:** `multipart/form-data` with two parts:

**Part 1: `metadata`** (application/json)
```json
{
  "client_id": "660e8400-e29b-41d4-a716-446655440000",
  "event_client_id": "550e8400-e29b-41d4-a716-446655440000",
  "ts": "2026-05-27T10:30:00Z",
  "lat": 43.6532,
  "lon": -79.3832,
  "device_p_on_device": 0.85,
  "model_id": "road_gate_stub_v1",
  "detections": [
    {"class_id": 0, "confidence": 0.85, "bbox": [0.1, 0.2, 0.5, 0.6]}
  ]
}
```

**Part 2: `frame`** (image/jpeg)
- Binary JPEG data
- Maximum size: 10 MB
- Must have valid JPEG magic bytes (FF D8 FF)
- **Metadata is stripped before the JPEG is stored** (Phase 2.6). EXIF/XMP (including any GPS
  IFD), maker notes, IPTC and comments are removed losslessly — the scan data is copied byte for
  byte, so the stored image is pixel-identical. The colour-relevant segments (JFIF, ICC, Adobe)
  are kept. Uploads are unaffected: send whatever the camera produced.

**Metadata Field Validation:**

| Field | Type | Required | Constraints |
|---|---|---|---|
| `client_id` | string | yes | 1–64 chars |
| `event_client_id` | string | no | Max 64 chars |
| `ts` | string | yes | ISO-8601 UTC |
| `lat` | float | yes | [-90, 90] |
| `lon` | float | yes | [-180, 180] |
| `device_p_on_device` | float | yes | [0, 1] |
| `model_id` | string | no | Max 128 chars |
| `detections` | array | no | Array of detection objects |

**Response 200:**
```json
{
  "client_id": "660e8400-e29b-41d4-a716-446655440000",
  "server_p": null,
  "label": null,
  "model_id": null
}
```

`server_p`, `label`, and `model_id` are null until the fusion engine processes the frame (Phase 2.1).

**Error Responses:**
| Status | Condition |
|---|---|
| 400 | Invalid metadata JSON, invalid JPEG, or empty file |
| 413 | Frame exceeds MAX_FRAME_SIZE_BYTES |
| 429 | Rate limit exceeded (> 5000 frames/hour per device) |

---

## Database Schema

The schema uses **generic asset naming** (per enterprise-architecture-plan.md §5.2) for multi-asset extensibility:

| Table | Purpose |
|---|---|
| `asset_observation` | Sensor events (replaces `event` from roadmap) |
| `asset_frame` | Camera frames (replaces `frame`) |
| `asset_cluster` | Aggregated clusters (Phase 2.2) |
| `observation_cluster_link` | Many-to-many cluster membership |
| `fusion_run` | Fusion engine execution audit trail |
| `fusion_pair` | Observation↔frame pairing with fused confidence, the `match_cost` that selected it, and `is_primary` (the best view of that observation) |
| `asset_type_registry` | Lookup: asset_type → metadata |
| `device_rate_limit` | Persistent rate tracking (**unused** — the live limiter is in-memory per worker) |
| `sensor_model` | Versioned ported-MATLAB classifier; one active row (Phase 2.1) |
| `org` / `staff_user` / `org_member` / `refresh_token` | City-staff auth tier (Phase 2.4) |
| `repair_log` | Audit trail for cluster repair/reopen (Phase 2.5) |
| `model_disagreement` | device↔server probability gap, for Phase 3 review (Phase 2.3) |
| `schema_migrations` | Migration ledger — filename, checksum, applied_at (Phase 2.6) |
| `frame_label` | Human ground truth per frame: 1 pothole / 0 not / -1 unsure (Phase 2.7) |

`asset_observation.sensor_class_probs`, `asset_cluster.class_probs` and `asset_cluster.bearing_deg` (Phase 2.2c, `migrations/011`) carry the class distributions and heading that the spatiotemporal crowd fusion needs; see [`docs/phase-2.2c-spatiotemporal-fusion.md`](./docs/phase-2.2c-spatiotemporal-fusion.md).

`fusion_pair.match_cost` and `fusion_pair.is_primary` (Phase 2.2d, `migrations/012`) record which frame the pairing search chose for an observation and why. The search ranks candidates by a lookahead cost rather than by nearest-in-time, because the camera resolves a pothole while it is still ahead of the vehicle; see [`docs/phase-2.2d-pairing-search.md`](./docs/phase-2.2d-pairing-search.md) for the design and [`docs/phase-2.2d-runbook.md`](./docs/phase-2.2d-runbook.md) for the procedure.

`asset_cluster.org_id` (Phase 2.6, `migrations/009`) is the owning municipality, or `NULL` for
unowned. It scopes **repair writes** only; reads remain global. See
[`docs/phase-2.6-hardening.md`](./docs/phase-2.6-hardening.md) §6.

All geometry columns use `GEOGRAPHY(POINT, 4326)` with GIST indexes for spatial queries.

### Verifying the Database

```bash
# Connect to the local database
docker compose exec postgres psql -U pothole -d pothole_db

# Check tables exist
\dt

# Check PostGIS is enabled
SELECT PostGIS_Full_Version();

# Check spatial indexes
\di idx_asset_observation_geom
```

---

## Running Tests

> **The DB-backed fixtures TRUNCATE every table.** They run against a dedicated
> `pothole_test` database, never the `pothole_db` you collect drive data into.
> `tests/conftest.py` enforces this with an allow-list and fails loudly if
> `DATABASE_URL` points anywhere else — so an accidental `pytest` can't destroy a drive.

```bash
# One-time: create the test database (the container's init script only runs on
# first boot, so an existing volume needs this by hand).
docker compose up -d
docker compose exec postgres createdb -U pothole pothole_test

# Validation tests (no database required — DB tests skip)
pytest tests/ -v

# Full suite. tests/__init__.py defaults DATABASE_URL to pothole_test, so no
# environment variable is needed; export it only to target a different database.
pytest -q
```

---

## Project Structure

```
server/
├── app/
│   ├── __init__.py
│   ├── main.py              # FastAPI app, lifespan, CORS
│   ├── config.py            # Pydantic Settings (env vars)
│   ├── database.py          # asyncpg connection pool
│   ├── dependencies.py      # Shared dependencies (auth, version check)
│   ├── models/
│   │   ├── __init__.py      # Event Pydantic models
│   │   ├── events.py        # Re-exports
│   │   └── frames.py        # Frame Pydantic models
│   ├── routes/
│   │   ├── __init__.py
│   │   ├── health.py        # GET /health
│   │   ├── events.py        # POST /api/v1/events
│   │   └── frames.py        # POST /api/v1/frames
│   ├── middleware/
│   │   ├── __init__.py
│   │   └── rate_limit.py    # Per-device sliding-window limiter
│   └── services/
│       ├── __init__.py
│       ├── event_service.py  # Event batch insert logic
│       └── frame_service.py  # Frame storage + DB insert
├── migrations/
│   └── 001_initial_schema.sql  # PostGIS DDL
├── tests/
│   ├── __init__.py           # Test fixtures and helpers
│   ├── conftest.py           # Pytest fixture exports
│   ├── test_events.py        # Event endpoint tests
│   ├── test_frames.py        # Frame endpoint tests
│   └── test_health.py        # Health endpoint tests
├── docker-compose.yml        # PostgreSQL + PostGIS, and the API service
├── Dockerfile                # Production container (node dashboard build + API)
├── .env.example              # Environment template
├── .gitignore
├── pyproject.toml            # Project metadata + tool config
├── requirements.txt          # Pinned dependencies
└── README.md                 # This file
```

---

## Scripts

All are run from the repo root and take `DATABASE_URL` from `.env`.

| Script | What it does |
|---|---|
| `scripts/create_staff.py` | Provision an org + staff account for the dashboard (Phase 2.4) |
| `scripts/seed_demo.py` | Synthetic clusters so the dashboard has something to render. **Refuses any database but `pothole_test`/`pothole_ci`** (Phase 2.5b) |
| `scripts/detect_eval.py` | Score stored frames with a given `.onnx` and report a histogram, annotated JPEGs, and (with `--labels`) precision/recall. **Writes nothing** (Phase 2.7) |
| `scripts/label_frames.py` | Localhost page for hand-labelling frames into `frame_label`. **Refuses `pothole_test`** — the fixtures TRUNCATE it (Phase 2.7) |
| `scripts/backfill_detection.py` | Run detection over already-uploaded frames, then clear `processed_at` so fusion re-scores the pairs (Phase 2.7) |
| `scripts/pairing_eval.py` | Measure the pairing search: `--diff` compares the old and new rankings, `--fit-lead` fits the camera's lead band from `frame_label`. Read-only (Phase 2.2d) |
| `scripts/requeue_frames.py` | Clear `processed_at` and re-run fusion over stored frames — the activation path after changing any `FUSION_*` pairing knob (Phase 2.2d) |

Note the two guards point in **opposite** directions, deliberately: `seed_demo.py` writes
fabricated data so it must never touch the real database, while `label_frames.py` writes ground
truth about real frames so it must never write to the one the tests wipe.

---

## Deployment — Supabase Production

### 1. Create Supabase Project

1. Go to [supabase.com](https://supabase.com) and create a new project in the **Canada** region.
2. Navigate to **SQL Editor** and run `migrations/001_initial_schema.sql`.
3. Navigate to **Storage** and create a bucket named `frames`.

### 2. Get Connection String

1. Go to **Settings → Database → Connection string** → select "Transaction" pooler.
2. Copy the connection string (format: `postgresql://postgres.[ref]:[password]@aws-0-[region].pooler.supabase.com:6543/postgres`).

### 3. Configure the Server

```bash
DATABASE_URL=postgresql://postgres.xxxx:your-password@aws-0-ca-central-1.pooler.supabase.com:6543/postgres
DATABASE_USE_POOLER=true
STORAGE_BACKEND=supabase
SUPABASE_URL=https://xxxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...your-service-role-key
SUPABASE_STORAGE_BUCKET=frames
ENV=production
CORS_ORIGINS=https://your-dashboard-domain.com
```

### 4. Deploy

The Dockerfile is production-ready. Deploy to any container host:

```bash
docker build -t pothole-server .
docker run -p 8000:8000 --env-file .env pothole-server
```

Recommended hosts: Fly.io, Railway, Cloud Run, or any container service.

---

## Testing with the Mobile App

Point the mobile app at your server:

1. Set `POTHOLE_API_BASE_URL=http://<your-server-ip>:8000` in `secrets.properties`.
2. Build and run the app: `gradlew.bat :app:installDebug`
3. Trigger a detection (shake the device or drive over a bump).
4. Verify the event appears in the database:

```sql
SELECT client_id, device_id, ts_utc, ST_AsText(geom::geometry)
FROM asset_observation
ORDER BY received_at DESC
LIMIT 5;
```

---

## Design Decisions

| Decision | Rationale |
|---|---|
| **asyncpg** over SQLAlchemy | Direct control over queries, PostGIS support, no ORM overhead for simple inserts |
| **ON CONFLICT DO NOTHING** | Idempotent uploads — mobile retries don't create duplicates |
| **Generic table naming** | `asset_observation` supports multi-asset extensibility (Phase 5) without migration |
| **In-memory rate limiter** | Simple for single-instance; documented Redis upgrade path for scaling |
| **No ORM** | Raw SQL gives precise control over PostGIS functions and ON CONFLICT behavior |
| **No Alembic** | Raw SQL migrations for simplicity; add Alembic when migration count grows |
| **Pydantic v2** | Fast validation with clear error messages matching mobile wire format |
| **Statement cache disabled** | Required for Supabase connection pooler (pgbouncer transaction mode) |

---

## Mobile Wire-Format Compatibility

This server implements the **frozen v1 wire contract** documented in:
- `docs/phase-1-changes.md` §5 (sensor events)
- `docs/phase-1.5-changes.md` §14 (camera frames)

The mobile client (`PotholeApi.java`, `UploadEventsWorker.java`) sends exactly the payloads this server expects. No mobile code changes are required.

Key compatibility guarantees:
- `Accept-Version: v1` header enforced on all ingestion endpoints
- Partial batch acceptance applies to **database** errors only: a row that fails to insert is
  reported in `rejected` while the rest of the batch succeeds. **Schema violations behave
  differently** — Pydantic validates `list[EventPayload]` as a whole, so one out-of-range field
  returns 422 for the entire batch. Keep the field bounds wide enough that real data cannot trip
  them; see `docs/road-test-readiness.md` §3.
- Optional fields (absent vs null) handled correctly — Pydantic treats both as None
- Duplicate uploads are idempotent (200 response, ID in `accepted`)
- Client deletion logic: only IDs in `accepted` are deleted from local Room
