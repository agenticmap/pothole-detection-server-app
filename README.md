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

This launches PostgreSQL 16 with PostGIS on **`localhost:5433`** (compose maps host 5433 -> container 5432; the service is named `postgres`). The migration SQL (`migrations/001_initial_schema.sql`) runs automatically on first boot via the `docker-entrypoint-initdb.d` volume mount.

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
| `RATE_LIMIT_EVENTS_PER_HOUR` | `100` | Max events per device per hour |
| `RATE_LIMIT_FRAMES_PER_HOUR` | `100` | Max frames per device per hour |
| `MAX_BATCH_SIZE` | `100` | Max events in a single POST batch |
| `MAX_FRAME_SIZE_BYTES` | `10485760` | Max JPEG size (10 MB) |

---

## API Endpoint Contracts

### `GET /health`

Health check endpoint.

**Response 200:**
```json
{
  "status": "healthy",
  "db": "connected",
  "version": "2.0.0"
}
```

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

| Field | Type | Required | Constraints |
|---|---|---|---|
| `client_id` | string | yes | 1–64 chars, UUID format |
| `schema_version` | int | no | Default 1, range [1, 100] |
| `ts` | string | yes | ISO-8601 UTC with timezone |
| `lat` | float | yes | [-90, 90] |
| `lon` | float | yes | [-180, 180] |
| `speed_mps` | float | yes | [0, 200] |
| `bearing_deg` | float | yes | [0, 360] |
| `speed_accuracy_mps` | float | no | [0, 200] |
| `accel_max_g` | float | yes | [-50, 50] |
| `accel_std` | float | yes | [0, 100] |
| `magnitude` | float | yes | [0, 500] |
| `gbar_in_max` | float | no | [0, 500] |
| `time_in_max` | float | no | ≥ 0 |
| `time_in_min` | float | no | ≥ 0 |
| `confidence` | float | yes | [0, 1] |
| `raw_window_b64` | string | no | Max 50,000 chars |
| `visual_confirmed` | bool | no | — |
| `frame_client_id` | string | no | Max 64 chars |

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
| 429 | Rate limit exceeded (> 100 events/hour per device) |
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
| 429 | Rate limit exceeded (> 100 frames/hour per device) |

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
| `fusion_pair` | Observation↔frame pairing with fused confidence |
| `asset_type_registry` | Lookup: asset_type → metadata |
| `device_rate_limit` | Persistent rate tracking (future) |

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

```bash
# Validation tests (no database required)
pytest tests/ -v

# With database running (full integration tests)
docker compose up -d
pytest tests/ -v
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
├── docker-compose.yml        # Local PostgreSQL + PostGIS
├── Dockerfile                # Production container
├── .env.example              # Environment template
├── .gitignore
├── pyproject.toml            # Project metadata + tool config
├── requirements.txt          # Pinned dependencies
└── README.md                 # This file
```

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
