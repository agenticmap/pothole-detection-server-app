---
updated: 2026-08-23
---

# Enterprise Architecture & Development Plan
## Pothole Detection → Municipal Asset Tracking Platform

> Companion document to [`docs/roadmap.md`](./roadmap.md). The roadmap describes the on-device mobile app and its server-side counterpart in broad strokes. This document defines the **enterprise-grade server platform, real-time GIS dashboard, modular fusion-engine integration, and multi-asset extensibility strategy** that the app feeds into, beginning at Phase 2.
>
> **Scope of this document:** strategy and architecture only. No application code. Backend logic, frontend components, and database queries are intentionally excluded at this stage.

---

## 0. Executive Summary

**Vision.** Evolve the existing pothole-detection mobile app (Phases 1, 1.5, 1.6) into a federated municipal asset-tracking platform suitable for adoption by municipalities and provincial transportation authorities (e.g., Ontario MTO). The mobile client is the data-producer; the new server platform is the system of record; the new web GIS dashboard is the operator-facing surface.

**Three architectural commitments anchor everything below:**

1. **The mobile wire contract is frozen at v1.** Phase 1.5/1.6 wire formats (`Accept-Version: v1`, `POST /events`, `POST /frames`) are load-bearing. Server changes must preserve them — `v2` is an additive overlay, never a breaking rewrite.
2. **The data schema is portable PostGIS.** Whatever managed offering we deploy on, the underlying tables are plain SQL + PostGIS. Vendor switch cost is bounded to *compute* and *API surface*, not data.
3. **The asset model is generic from day one.** `pothole_cluster` will be renamed `asset_cluster` (with an `asset_type` discriminator) before public data is committed. Designing for "potholes only" now produces a costly rewrite in Year 2.

**Three top-level recommendations:**

| Decision | Recommendation |
|---|---|
| Mobile ingestion protocol | **Keep HTTP + WorkManager.** Do not migrate to WebSockets/MQTT for the upload path. Reserve streaming protocols for server→dashboard push only. |
| Database backend | **Supabase for Phase 2–3; pre-architect a clean migration path to self-hosted Postgres + custom API for Phase 4.5 enterprise deployments.** |
| Dashboard stack | **MapLibre GL JS + Martin (Rust) MVT tile service + materialized PostGIS aggregations + WebSocket delta push.** |

---

## 1. Mobile Ingestion & Payload Review Framework

Before the server platform is built, the **upstream contract must be audited** from the perspective of *zero data loss under adverse network conditions*. This section is a checklist for that audit. It is the first deliverable required before Phase 2 server work begins.

### 1.1 Source documents to review (in order)

Each builds on the previous. The doc may lag the code — when they disagree, **the code wins**.

| Order | Document / File | What to extract |
|---|---|---|
| 1 | `docs/phase-1-changes.md` §5 (REST contract) | Wire format for sensor events: field-by-field semantics, server-rejection rules, idempotency contract. |
| 2 | `docs/phase-1-changes.md` §4 (Upload pipeline) | WorkManager coalescing, ExistingWorkPolicy.KEEP, periodic safety-net behavior. |
| 3 | `docs/phase-1.5-changes.md` §14 (Frames endpoint) | Multipart frame upload, `event_client_id` linkage slot, gzip behavior. |
| 4 | `docs/roadmap.md` Phase 2 §2.2, §2.6 | Target server-side schema and read-back contract. |
| 5 | `app/src/main/java/.../work/UploadEventsWorker.java` | Actual retry/backoff behavior, deletion-on-success policy. |
| 6 | `app/src/main/java/.../network/PotholeApi.java` | Headers, timeouts, error coercion, OkHttp configuration. |
| 7 | `app/src/main/java/.../data/EventRepository.java` | The persistence-before-upload invariant. |
| 8 | `CLAUDE.md` §"Hard invariants" | The 8 contracts already protected by tests. Do not regress these. |

### 1.2 Per-payload audit checklist

For each upload payload (`event`, `frame`, future `label`, future `feedback`):

#### Identity & idempotency
- [ ] Is `client_id` generated **before** the row is written to Room? (UUID v4 on insert is correct.)
- [ ] Is the server's primary key the same `client_id`? (Required — re-uploads after a 5xx must not double-insert.)
- [ ] Does the server reject duplicates with `409 Conflict` or silently `200`-no-op? Either is acceptable; **document the choice in the API spec** so the client deletion logic matches.
- [ ] Is the deletion-on-success policy: *Room row is deleted only after a 2xx response*? Verify in `UploadEventsWorker`.

#### Wire format
- [ ] Required headers present on every request: `Accept-Version`, `X-Device-Id`, `Content-Type`, `Content-Encoding` (where gzipped)?
- [ ] Timestamps are ISO-8601 with timezone offset (`TIMESTAMPTZ`-compatible), never naive local time?
- [ ] Geometry is shipped as `{lat, lon}` floats and reassembled server-side via `ST_MakePoint(lon, lat)` — *not* WKT, which is easy to poison?
- [ ] Floating-point fields have plausible bounds checked server-side? (`accel_max_g` ≤ ±50 g, `speed_mps` ≤ 100, etc. Reject anomalies; do not silently store.)

#### Transport reliability
- [ ] Persistence is decoupled from upload — Room first, worker later? (Phase 1 invariant #1.)
- [ ] WorkManager backoff is exponential, jittered, capped (recommended ceiling: 1 h)?
- [ ] 4xx responses are **not** retried (poison-message handling). 5xx and network errors **are** retried.
- [ ] `OneTimeWorkRequest` is coalesced via `ExistingWorkPolicy.KEEP` with a stable work name (`pothole_upload_one_shot`)?
- [ ] The hourly periodic safety-net worker is registered exactly once in `PotholeApp.onCreate`?
- [ ] Worker constraints are explicit (network type, battery state)? "Default constraints" is a hidden coupling waiting to break.

#### Payload size & frequency
- [ ] Worst-case event burst rate is bounded by `PotholeDetectionService`'s detection cooldown? Document the cap.
- [ ] Frame persistence rate (post-throttle) is documented? CameraX ImageAnalysis runs at ≤ 8 fps; only `CONFIRM_HIT` and `AMBIGUOUS_QUEUE` decisions persist.
- [ ] 95th-percentile JPEG size is measured? Drives the per-device storage budget.
- [ ] Gzipped raw-window blob size is bounded? `[180, 10] × 4 bytes ≈ 7.2 KB` pre-compression; ~3 KB post-gzip.

#### Schema versioning
- [ ] `Accept-Version: v1` lets a `v2` server reject ambiguously-shaped payloads from a future client without silent corruption?
- [ ] Wire-format additions are nullable and backwards-compatible (CLAUDE.md invariant #8)?

### 1.3 Network-protocol decision: HTTP vs WebSockets vs MQTT

**Do not migrate from HTTP to WebSockets/MQTT for the upload path.** Recorded justification:

| Protocol | Fit for upload | Verdict |
|---|---|---|
| **HTTP POST + WorkManager queue (current)** | Bursty, retryable, offline-tolerant, idempotent. Native Android support. Each request is a discrete unit of work the OS can schedule. | **Keep.** This is the right protocol for store-and-forward client→server data ingestion. |
| WebSockets | Real-time bidirectional. Requires sustained connection, hostile to cellular battery, and incompatible with WorkManager's "wake when network appears" model. | **Reject for upload.** Reserve for server→dashboard live push (§3). |
| MQTT | Optimal for low-power IoT with a persistent broker. Overkill for Android-class devices uploading multi-MB camera frames. Adds a broker dependency. | **Reject.** No advantage over HTTP for our device class. |

**Where streaming protocols *do* belong in this system:**
- **Server → dashboard:** WebSocket push for live "new cluster appeared" events (Section 3).
- **Server → server (internal):** gRPC between API and fusion-engine sidecar (latency-sensitive).
- **App → server:** HTTP remains.

### 1.4 Zero-data-loss guarantees — what to verify exists

Each of the following must have an explicit, documented owner. None of them are "trust the worker":

1. **Local durability before transmission.** `EventRepository.recordEvent` writes to Room synchronously on a background thread, then enqueues the upload.
2. **Idempotent server insert.** `client_id` as PK + `ON CONFLICT DO NOTHING` (or update-if-newer semantics for mutable fields).
3. **Deletion only on 2xx.** The worker must not delete the Room row before the server acknowledges.
4. **Bounded backoff.** Exponential, capped at ~1 h, jittered to avoid thundering herd after outages.
5. **Schema-version pinning.** `Accept-Version: v1` prevents silent format drift across client/server versions.
6. **Server-enforced storage budget.** Per-device cap (proposed: 500 MB JPEGs, Phase 2 §2.8). Client learns the cap via `413 Payload Too Large` and sheds locally — does not loop forever.
7. **Clock-skew tolerance.** Server records `received_at = now()` independently of client `ts_utc`. Fusion math uses client `ts_utc`; ops/analytics use server `received_at`. Both stored.

### 1.5 Audit deliverable

A 1–2 page **Mobile Ingestion Conformance Report** with:
- Per-checklist-item status: `PASS` / `FAIL` / `GAP`.
- Each `GAP` sized in app-day-equivalents.
- A signed-off "wire format frozen at v1.x" git tag before Phase 2 server work begins.

This report is a **gate** for Phase 2.0.

---

## 2. Database Architecture Strategy: Supabase vs Postgres + Custom API

### 2.1 Decision framing

`docs/roadmap.md` §2.1 tentatively chose Supabase. This section re-examines that choice under the enterprise/municipal lens introduced by the new requirements: **real-time GIS dashboard, MTO/municipal customers, modular fusion engine, multi-asset extensibility.**

The decision is not binary. It is a **deployment-target spectrum** with two anchor points:

| Anchor | What | Right for |
|---|---|---|
| **A. Supabase (managed BaaS)** | Postgres 15 + PostGIS + PostgREST + Realtime + Edge Functions + Storage, all hosted by Supabase. | MVP, pilot, < 10 municipalities, prototype to first paying customer. |
| **B. Self-hosted Postgres + custom API** | Postgres + PostGIS on VPC / on-prem; FastAPI or Go services for read/write; Redis or NATS for realtime; MinIO or S3 for blobs. | Enterprise / government contracts; FedRAMP / PIPEDA / Ontario gov-cloud; > 50k devices; bespoke SLOs. |

### 2.2 Dimension-by-dimension comparison

| Dimension | Supabase | Postgres + Custom API | Edge |
|---|---|---|---|
| **PostGIS support** | Native (extension preloaded). | Native (same extension). | Tie. |
| **Scaling — read** | PostgREST fine to ~1k req/s; read replicas on paid plan. | Whatever you build. Shard reads, partition by region, add replicas. | **Custom** at scale (>10k req/s); **Supabase** below. |
| **Scaling — write** | Single primary, vertical scale to large tiers. | Same Postgres constraints, but you control partitioning (e.g., `asset_observation` partitioned by month). | **Custom** for write-heavy. Our writes are modest (estimated 1k–10k events/hour at scale) — **either works** through Phase 2–3. |
| **Real-time streaming** | Logical replication → Realtime broadcast → WebSocket. Free: 200 channels / 100 msg/s. Paid: 500 channels. | DIY: `LISTEN/NOTIFY` + WebSocket gateway, or Redis pub/sub, or NATS. Unbounded. | **Supabase** for MVP; **Custom** for high fan-out (>500 simultaneous operators). |
| **Row-level security** | First-class. SQL policies; PostgREST enforces automatically. | Postgres RLS works identically; you must run every API query as the authenticated role. | **Supabase** for dev velocity; **Custom** can match but it's your code. |
| **Auth** | Built-in: email/password, OAuth, magic-link, JWT. | DIY (Auth0 / Keycloak / Cognito + middleware). | **Supabase** — significant time savings. |
| **Storage (JPEGs)** | Supabase Storage (S3-compatible behind a CDN). | S3 / R2 / MinIO — same wire API. | Functional tie; **Supabase** for setup speed. |
| **Heavy GIS workloads** (tile gen, raster, routing) | PostgREST is awkward for MVT output — front PostGIS with Martin / pg_tileserv anyway. Edge Functions can't host Martin. | Run Martin as a sidecar pointing at the same DB. Trivial. | **Custom** — but Supabase can be combined with an external Martin instance for the best of both. |
| **Worker compute** (fusion, server-side YOLO) | Edge Functions: Deno, no GPU, ~150 s/exec. Need external Modal / Replicate / Cloud Run for inference. | Anything: Python on Cloud Run, Go on Fly, Triton on GKE. GPU at will. | **Custom** for ML pipelines. Supabase Edge Functions are cron triggers + light orchestration only. |
| **Dev velocity** | Days to first ingestion endpoint. Auth + RLS + Realtime free out of the box. | Weeks of platform plumbing. | **Supabase** decisively. |
| **Operational burden** | Backups, replication, failover — Supabase's problem. | All yours. PG ops is a job. | **Supabase** decisively. |
| **Cost at small scale** | Free tier covers MVP. Pro at $25/mo. | $5–30/mo for small Postgres + a couple of Cloud Run services. | Roughly tied; slight Custom edge (no usage-based surprises). |
| **Cost at enterprise scale** | Tier costs climb; storage egress + Realtime quotas become line items. | Linear cloud spend; reserved instances cut bills ~40%. | **Custom** at >100k devices. |
| **Vendor lock-in (data)** | None — `pg_dump` exports cleanly. | None. | Tie. |
| **Vendor lock-in (platform)** | Realtime, Auth, Edge Functions have proprietary wire surfaces. RLS policies are portable. | None. | **Custom**, but the Supabase-specific surface is small if architected with replacement in mind (§2.3). |
| **Government / municipal compliance** | SOC 2 Type II. Canada region supported. **No FedRAMP, no CJIS, no certified Ontario gov-cloud presence as of Q1 2026.** Platform itself is US-controlled. | Deployable to AWS GovCloud, Azure Gov, OVHcloud Canada, on-prem. Compliance is whatever the deployment is certified to. | **Custom** is the *only* option for contracts mandating domestic-controlled infrastructure. **Confirm with each customer.** |
| **MATLAB fusion engine integration** | Edge Functions can't host MATLAB Runtime or Octave — call an external sidecar. | Host MATLAB Runtime, Octave, or a Python port directly in your service mesh. | **Custom** is cleaner; **Supabase** workable via external sidecar. |
| **Multi-asset extensibility** | Schema change — same effort either way. | Same. | Tie. The platform doesn't constrain the data model. |

### 2.3 Recommendation: Two-phase deployment

> **Phase 2 → Phase 3: Supabase.**
> **Phase 4.5 (enterprise readiness onward): Postgres + custom API on a deployment target chosen per-customer.**

**Reasoning:**

1. **Velocity dominates Phase 2.** Phase 2's job is to prove the end-to-end loop (detect → upload → fuse → cluster → display) on real roads with real users. Supabase compresses 4–8 weeks of platform work into days. The on-device app is the product's heart; do not divert engineering capacity to platform plumbing prematurely.

2. **Phase 2 schema is portable.** Every table proposed in `docs/roadmap.md` §2.2 is plain PostGIS. Migrating off Supabase later is `pg_dump → pg_restore` + reimplementing RLS as API middleware + replacing Realtime with NATS/Centrifugo. Days to weeks, not months.

3. **MATLAB fusion lives outside Supabase regardless.** Whether the DB is Supabase or self-hosted, the fusion engine runs as an external sidecar (Modal / Cloud Run / Fly). The fusion architecture is the **same** under both anchors — see §5.

4. **Municipal compliance is a Phase 4 gate, not a Phase 2 one.** Pilot deployments will tolerate (and often prefer) "data hosted in Supabase Canada region" for a 6–12 month proof-of-value. Binding production contracts will require migration; the architecture must keep that exit clean.

**The single architectural rule that makes this real:**

> **Treat Supabase as four independent services — Postgres, Storage, Auth, Realtime — and wrap each behind a thin internal interface.**

This is the only code-level discipline needed in Phase 2 to keep the Phase 4.5 escape hatch open. The wrapping is hours of work; the freedom it preserves is months.

### 2.4 Override conditions

Pick **Custom from day one** if any of the following holds at project start:

- A signed letter of intent from MTO (or equivalent) is in hand **and** the contract terms require domestic-controlled infrastructure.
- Inference cost already dominates Postgres cost by an order of magnitude (i.e., GPU pipelines are running and Postgres is a rounding error).
- The team includes ≥ 1 dedicated platform engineer. Otherwise Supabase pays for itself in person-months saved.

Pick **Supabase forever** if:
- The product never crosses ~10k active devices.
- No customer ever demands certified on-prem.
- The team stays at 1–3 engineers.

---

## 3. GIS Dashboard Architecture

The dashboard is for **municipal operators**: dispatch supervisors, asset managers, repair-crew schedulers. Distinct from the mobile app (citizen-facing data producer), this is a **read-heavy, visualization-first** surface that must perform on day-old laptops in city offices.

### 3.1 Architectural principles

1. **Vector tiles, not raster tiles.** Vector tiles render client-side, scale at any zoom without re-fetching, and let us restyle ("repaired" vs "open") without server changes.
2. **Aggregate server-side, render client-side.** PostGIS `ST_ClusterDBSCAN` + `ST_AsMVT` do the heavy lifting; the browser draws.
3. **Push deltas, never refresh.** A dashboard showing 5,000 markers must not re-fetch all 5,000 every minute. WebSocket-pushed deltas only.
4. **Bounded query cost.** Every dashboard query is bbox-bounded. No "list all potholes in the province."
5. **The dashboard is a viewer, not a writer (initially).** Phase 4 adds repair-marking via the UI; Phase 2 read-only is enough for the pilot.

### 3.2 Layered architecture

```
┌──────────────────────────────────────────────────────────────────┐
│  Browser  (Municipal Operator)                                   │
│  ┌────────────────────────────────────────────────────────────┐  │
│  │  MapLibre GL JS  (vector-tile renderer, WebGL)             │  │
│  │  ├─ Base layer:        Protomaps PMTiles (self-hosted)     │  │
│  │  ├─ Cluster layer:     /tiles/clusters/{z}/{x}/{y}.mvt     │  │
│  │  ├─ Asset layer:       /tiles/observations/{z}/{x}/{y}.mvt │  │
│  │  ├─ Recent-frame layer:/tiles/frames/{z}/{x}/{y}.mvt       │  │
│  │  └─ WebSocket overlay: live new-cluster events             │  │
│  │                                                            │  │
│  │  Sidebar: filters (date range, severity, repaired toggle,  │  │
│  │           asset_type)                                      │  │
│  │  Details panel: cluster details, JPEG thumbnails           │  │
│  └────────────────────────────────────────────────────────────┘  │
└────────────────────┬──────────────────────────┬──────────────────┘
                     │ HTTPS (MVT)              │ WSS (deltas)
                     ▼                          ▼
┌──────────────────────────────────────────────────────────────────┐
│  Edge / CDN layer   (Cloudflare)                                 │
│  - Caches MVT tiles by (layer, z, x, y, filters_hash)            │
│  - 60 s TTL for cluster tiles; 300 s for repaired-cluster layer  │
│  - Bypasses cache for /api/v1/clusters/{id} (details)            │
└────────────────────┬──────────────────────────┬──────────────────┘
                     │                          │
                     ▼                          ▼
┌────────────────────────────────┐  ┌──────────────────────────────┐
│  Tile Service  (Martin, Rust)  │  │  WebSocket Gateway           │
│  - MVT generation from PostGIS │  │  - Subscribes to Postgres    │
│  - Functions defined as PL/pg- │  │    LISTEN/NOTIFY OR Supabase │
│    SQL: clusters_mvt(z,x,y,    │  │    Realtime                  │
│    filters jsonb)              │  │  - Per-municipality auth     │
│  - Stateless; horizontally     │  │  - Pushes JSON deltas        │
│    scaled                      │  │                              │
└────────────────────┬───────────┘  └──────────────┬───────────────┘
                     │                              │
                     └──────────────┬───────────────┘
                                    ▼
            ┌─────────────────────────────────────┐
            │   PostgreSQL 15 + PostGIS           │
            │   (Supabase  OR  self-hosted)       │
            │                                     │
            │   Core tables (see §5.2):           │
            │   - asset_observation, asset_frame, │
            │     asset_cluster, fusion_pair,     │
            │     fusion_run, repair_log          │
            │                                     │
            │   Materialized views per zoom band: │
            │   - cluster_tile_mv_z10/14/18       │
            │                                     │
            │   Triggers: NOTIFY on cluster       │
            │   INSERT/UPDATE                     │
            └─────────────────────────────────────┘
```

### 3.3 Component rationale

**Client: MapLibre GL JS**
- Open-source fork of Mapbox GL JS pre-license-change. Zero per-MAU billing.
- WebGL-accelerated. Handles 50k+ features per layer with reasonable styling.
- Native MVT consumption.
- Alternatives considered:
  - *Mapbox GL JS* — feature parity but per-MAU pricing becomes a real budget line. Rejected.
  - *Leaflet* — raster-first; clustering at scale needs `Leaflet.markercluster` plugin and gets sluggish. Viable for an embedded read-only widget but not the primary dashboard.
  - *deck.gl / CesiumJS* — 3D-capable, overkill until asset heights matter (Phase 5+).

**Base tiles: Protomaps PMTiles (preferred) or OpenMapTiles self-hosted**

> **Implemented in Phase 2.5b.** A PMTiles archive is served from `/basemap` over HTTP range
> requests, with the style flavour themed from the same CSS tokens as the chrome. See
> [`phase-2.5b-dashboard-design.md`](./phase-2.5b-dashboard-design.md) §4.
- Protomaps: a single addressable PMTiles archive per province, served from S3/R2 via HTTP range requests. **No basemap tile server needed.** ~30 GB for all of Canada at z0–14.
- Avoids per-tile Mapbox / MapTiler billing.

**Tile service: Martin (Rust)**
- Generates MVT directly from PostGIS via SQL functions.
- Substantially faster than pg_tileserv (Go) for our workload class per published benchmarks.
- Stateless; trivially horizontally scaled behind a load balancer.
- Configuration is YAML pointing at SQL functions; no per-layer service code.

**Real-time push: WebSocket gateway**
- Ranked server-side options:
  1. **Supabase Realtime** (if on Supabase) — point the browser at it directly; zero infra. Watch the quota.
  2. **Self-hosted Centrifugo or NATS** + a thin gateway that bridges Postgres `LISTEN/NOTIFY` to WebSocket. ~1 day of work.
  3. *Build-your-own WebSocket server.* Avoid unless forced.

**Read API (non-tile): FastAPI (Python) or Fastify (Node)**
- Endpoints: cluster details, member events, frame thumbnails, audit log.
- **Python preferred** — same ecosystem as the fusion engine and the eventual MATLAB port. Reduces context-switching tax.

**Aggregation strategy: precomputed materialized views per zoom band**
- `cluster_tile_mv_z10` (province-wide overview), `cluster_tile_mv_z14` (city-wide), `cluster_tile_mv_z18` (street-level).
- Refreshed by the clustering cron (roadmap §2.5).
- Why not on-the-fly: at z10, a single tile may contain 50k observations; aggregating per request blows the 200 ms p95 budget.

### 3.4 Performance targets

| Metric | Target (p50) | Hard ceiling (p95) |
|---|---|---|
| Cold MVT tile request (z14) | < 80 ms | < 250 ms |
| Warm tile (CDN hit) | < 20 ms | < 60 ms |
| Cluster detail panel open | < 150 ms | < 400 ms |
| WebSocket delta latency (DB → browser) | < 2 s | < 8 s |
| Concurrent operators per municipality | 5 | 20 |
| Dashboard JS bundle (gzipped) | < 800 KB | < 1.2 MB |

### 3.5 Multi-asset extensibility from day one

The dashboard's "Pothole layer" must be the first **instance** of a generic "Asset Observation layer." Concretely:

- One generic table: `asset_observation` with `asset_type TEXT NOT NULL` discriminator (`'pothole' | 'sign' | 'tree' | 'streetlight' | …`).
- One generic clustering output: `asset_cluster` with the same discriminator.
- Tile endpoints templated: `/tiles/{asset_type}/{z}/{x}/{y}.mvt`.
- New asset types in Year 2 = a new ML model + a new row in `asset_type_registry`. **No schema change.**

This is the single largest deviation from the `pothole_cluster`-named schema in `docs/roadmap.md` §2.2. Recommendation: land Phase 2.0 with the generic naming, even though only `asset_type = 'pothole'` is populated at first. Backwards compatibility for v1 clients is provided via an API-side alias (§5.2).

---

## 4. Phased Implementation Roadmap

This roadmap **extends** [`docs/roadmap.md`](./roadmap.md); it does not replace it. App-side phases (1, 1.5, 1.6, and the on-device portions of 2/3/4) remain authoritative there. This section adds the **server-side, dashboard, and fusion-engine phases** and slots them against the existing mobile timeline.

### 4.1 Master timeline

> **⚠️ Numbering corrected 2026-08-16.** This table originally called the operator dashboard
> "P2.3" and pilot deployment "P2.4", which **collide with the phases the code actually
> shipped** — 2.3 is the server-side detection model and 2.4 is the staff auth tier. The code
> is authoritative. The dashboard is **2.5** and pilot deployment is **2.7**; the rows below
> are renumbered accordingly, and the section headings §4.5/§4.6 keep their old numbers only
> as anchors. The Status column was also stale — everything through 2.5 was still marked
> "Planned" long after it shipped.

| Phase | Where described | Track | Status | Duration |
|---|---|---|---|---|
| **P1.6 ✱** | `roadmap.md` §"Phase 1.6" | Mobile | ✅ Shipped | (existing) |
| **P2.0 — Schema freeze + ingestion server** | This doc §4.2; refines `roadmap.md` §2.1–§2.2, §2.6 | Server | ✅ Shipped | 2–3 weeks |
| **P2.1 — Fusion engine v1 (MATLAB port)** | This doc §4.3 (new) | Server / ML | ✅ Shipped — `phase-2.1-fusion-engine-plan.md` | 2–3 weeks |
| **P2.2 — Clustering + read API** | This doc §4.4; refines `roadmap.md` §2.5–§2.6 | Server | ✅ Shipped — `phase-2.2-clustering-plan.md`, `phase-2.2b-read-path-plan.md` | 1–2 weeks |
| **P2.3 — Server-side detection model** | `roadmap.md` §2.3 | Server / ML | ✅ Shipped (gated off) — `phase-2.3-detection-plan.md`; enablement path (eval, ground-truth labels, backfill, ROI crop) built in `phase-2.7-detection-enablement.md`, still awaiting a model artifact | — |
| **P2.4 — City-staff auth tier** | `phase-2.4-auth-plan.md` | Server | ✅ Shipped | — |
| **P2.5 — Operator dashboard MVP** | This doc §4.5 (was "P2.3") | Web + Server | ✅ Shipped — `phase-2.5-dashboard-plan.md` | 3–4 weeks |
| **P2.6 — Production hardening** | `phase-2.6-hardening.md`; refines `roadmap.md` §2.8–2.10 + `road-test-readiness.md` "Known gaps" | Server / Ops | 🟡 In progress — startup-path fixes, container deployment and the Phase 2.5 security leftovers landed; §2.8 abuse/throughput work outstanding | — |
| **P2.7 — Pilot deployment** | This doc §4.6 (was "P2.4") | Ops | 📋 Planned | 2 weeks |
| **P3 ✱** | `roadmap.md` §"Phase 3" | Mobile | 📋 Planned | (existing) |
| **P3.5 — Fusion engine v2 (refined weights, async retraining)** | This doc §4.7 (new) | ML | Rolling | rolling |
| **P4 ✱** | `roadmap.md` §"Phase 4" | Mobile + Ops | 📋 Planned | (existing) |
| **P4.5 — Enterprise deployment** | This doc §4.8 (new) | Platform | Per-customer | per-contract |
| **P5 — Multi-asset expansion** | This doc §4.9 (new) | Mobile + Server | 📋 Planned | 6+ months |

✱ = unchanged from existing roadmap.

### 4.2 Phase 2.0 — Schema freeze + ingestion server (2–3 weeks)

**Objectives**
1. Provision the Postgres / Supabase target chosen per §2.3.
2. Apply DDL for the schema in `roadmap.md` §2.2, **modified for asset-genericism** (`asset_observation`, `asset_cluster`, `asset_type` discriminator).
3. Stand up the write side of the v1 REST contract: `POST /events`, `POST /frames`, `POST /labels` (stub, gated off).
4. Cut a mobile release pinned to the v1 wire format. **Freeze.**

**Exit criteria**
- An installed APK posts events + frames to staging; rows appear in Postgres within 5 s.
- `POTHOLE_API_BASE_URL=""` build path still works end-to-end (Phase 1 invariant #2 regression test passes).
- All 8 Phase 1 hard invariants in `CLAUDE.md` re-verified.
- Mobile Ingestion Conformance Report (§1.5) signed off.

**Risks**
- Schema renaming (`pothole_cluster` → `asset_cluster`) is a one-shot. Doing it later costs 10×.
- Supabase free-tier limits — keep an eye on storage egress during pilot.

### 4.3 Phase 2.1 — Fusion engine v1 (MATLAB port) (2–3 weeks, gated)

This is the integration point for your MATLAB sensor-fusion methodology. **No code yet** — the backend data structures must be designed to accept it cleanly *before* the engine itself ships.

**Pre-conditions (gated on MATLAB code review in the next turn)**

Once the MATLAB code arrives, it must be classified into one of three buckets, because each requires a different deployment shape:

| Bucket | What | Deployment shape |
|---|---|---|
| **(a) Deterministic math transform** | Closed-form formulas, filter banks, FFTs, state-space updates. | Port to Python + NumPy/SciPy in days. Run in the API service or fusion-engine sidecar. |
| **(b) Trained ML model** | Anything with learned weights (NN, SVM, regression). | Export to ONNX. Run via ONNXRuntime — no MATLAB Runtime needed in production. |
| **(c) Heavy algorithm depending on MATLAB-specific functions** | Toolbox-dependent (Signal Processing, Sensor Fusion). | Containerize MATLAB Compiler Runtime (or Octave if function-compatible). Run as a separate gRPC sidecar. |

**Stable backend contract for the fusion engine (independent of bucket)**

The fusion engine is addressed as a **black-box function** with this interface (described, not implemented):

- **Inputs:**
  - `sensor_window` — `float[180, 10]` array after rotation correction (the existing `raw_window_blob` shape).
  - `visual_detections` — list of YOLO bbox + confidence + class_id records.
  - `metadata` — speed_mps, bearing_deg, dt_ms_between_event_and_frame, dist_m_between, GPS accuracy.
- **Outputs:**
  - `fused_confidence` — float in [0, 1].
  - `severity` — float (model-specific units; documented per version).
  - `feature_vector` — optional embedding for downstream clustering or audit.
  - `debug` — `{model_id, engine_version, weights_used, runtime_ms}`.

The API layer never calls into MATLAB directly. The fusion engine is an internal sidecar exposed via gRPC. Three swap-in implementations live behind that interface:
- `fusion.python_v1` — heuristic baseline from `roadmap.md` §2.4 (sigmoid-weighted).
- `fusion.matlab_port_v1` — your methodology, ported per (a)/(b)/(c).
- `fusion.cnn_v3` — Phase 3 learned-fusion model.

**Backend data structures to provision in Phase 2.0 (ahead of the engine arriving)**

| Table | Purpose | Why pre-allocated |
|---|---|---|
| `fusion_run` | One row per fusion-job execution: `run_id`, `engine_version`, `weights_jsonb`, `started_at`, `completed_at`, `inputs_count`, `outputs_count`. | Audit trail: which engine produced which `fused_confidence`. |
| `fusion_pair` (already in roadmap §2.2) | One row per `(observation, frame)` pairing with `fused_confidence`, `delta_ms`, `delta_m`. **Add `fusion_run_id` FK** so history can be re-run with a new engine and compared. | A/B comparison across engine versions. |
| `raw_window_blob` (storage bucket) | Gzipped `[180, 10]` arrays referenced by `asset_observation.raw_window_url`. | Fusion engine input. Ingestion must store this; it already does. |
| `fusion_engine_metrics` (new) | Per-run: TP count, FP-estimate, runtime ms, memory peak, disagreement rate vs prior version. | Objective engine-version comparison. |

**Exit criteria**
- A test fusion run on 1k staged observations × 1k staged frames produces deterministic, byte-identical output across two consecutive runs on the same data.
- `fusion_pair` rows are queryable by the clustering job.
- Engine version is tagged and rollback-able (revert `fusion_run.engine_version`; clustering re-derives).

### 4.4 Phase 2.2 — Clustering + read API (1–2 weeks)

**Objectives**
1. Implement `ST_ClusterDBSCAN`-based clustering per `roadmap.md` §2.5 (already designed).
2. Implement `GET /api/v1/potholes?bbox=…&zoom=…` per `roadmap.md` §2.6 (alias for `GET /api/v1/observations?type=pothole&…`).
3. Front with Cloudflare; configure caching per §3.3.
4. Verify per `roadmap.md` §2.10 (synthetic + load + manual).

**Differences from existing roadmap §2.5**
- Generic `asset_cluster` naming (carryover from §4.2).
- Materialized views per zoom band (§3.3).

### 4.5 Phase 2.5 — Operator dashboard MVP (3–4 weeks)

> **Status: shipped.** As-built and the deviations
> from Section 3 (FastAPI `ST_AsMVT` instead of Martin, no materialized views, no CDN, no
> WebSocket yet) are recorded in [`phase-2.5-dashboard-plan.md`](./phase-2.5-dashboard-plan.md).
> Phase numbered 2.5, not 2.3 — see the note on §4.1.

Build the GIS dashboard per Section 3.

**Scope cuts for MVP**
- ~~Read-only. No repair marking via UI; admins use Supabase Studio or direct SQL.~~
  **Changed:** repair marking ships with the dashboard as
  `POST /api/v1/clusters/{id}/repair` (staff-gated, audited in `repair_log`). There is no
  Supabase Studio in this deployment, so the alternative was hand-editing `repaired_at` on a
  live database.
- Single-municipality scope (filtering UI exists, but auth model is "all operators see all data").
  **Still true, and now load-bearing:** `asset_cluster` has no `org_id`, so any staff member of
  any org can repair any city's clusters.
- Desktop-first; mobile-responsive is nice-to-have, not required.

**Exit criteria**
- Operator pans/zooms a city, sees clusters, clicks for details, sees member frames. *(Done.)*
- p95 tile fetch < 250 ms on a residential connection.
  *(Server side measured at p95 27 ms for z14 and 81 ms for aggregated z10 on 20k clusters.)*
- Live "new cluster appeared" toast within 8 s of server-side detection in pilot data.
  *(Deferred. Clustering only runs every 15 minutes, so a sub-8-second push target is largely
  theatre; `updated_at` polling is the honest MVP and the index for it already exists.)*

### 4.6 Phase 2.7 — Pilot deployment (2 weeks)

**Objectives**
- Identify one municipality willing to pilot. Realistic entry points:
  - A Boston-area municipality via Northeastern partnerships.
  - An Ontario municipality via Northeastern's Canadian alumni / MTO research-partnership channels.
  - A smaller GTA municipality (more receptive to research pilots than MTO itself for v0).
- Deploy production-grade (staging-graduated) Supabase project with monitoring.
- Onboard 1–3 operator accounts.
- Distribute the mobile APK to ~25 test users on real roads in the pilot city.

**Success metrics**
- 1k+ confirmed pothole detections within the pilot city's bounds within 4 weeks.
- Operator NPS and qualitative feedback captured.
- Zero data-loss incidents.

### 4.7 Phase 3.5 — Fusion engine v2 (rolling, gated on labeled data)

Once Phase 3's labeled-data flywheel produces enough training data, the MATLAB-port engine can be re-tuned (weight updates) or replaced (with a learned fusion model). The §4.3 engine interface is unchanged — only the implementation behind it swaps. The `fusion_run` audit trail makes the rollout auditable and reversible.

### 4.8 Phase 4.5 — Enterprise deployment (per-customer)

Triggered by a signed contract requiring on-prem or gov-cloud.

**Objectives**
1. Stand up Postgres + custom API per §2.3 Anchor B in the customer's required region / cloud.
2. Migrate data via `pg_dump` / logical replication.
3. Reimplement RLS as API middleware (the only Supabase-specific surface that doesn't lift cleanly).
4. Replace Supabase Realtime with NATS / Centrifugo.
5. Re-certify compliance: SOC 2, PIPEDA, customer-specific (FedRAMP, CJIS, Ontario PSDA).

**Estimated cost** if §2.3's wrapping discipline was followed in Phase 2: **4–8 engineering weeks**. If not: **4–8 engineering months**. This delta is the entire point of the discipline.

### 4.9 Phase 5 — Multi-asset expansion (6+ months)

**Objectives**
- Generalize the camera pipeline: `RoadGateModel` becomes `ObjectDetectionModel`; the `asset_type_registry` maps model output classes to `asset_observation.asset_type`.
- Each new asset type gets: a YOLO class fine-tune, a server-side validation model, an operator UI category, and a repair workflow.

**Suggested rollout order after potholes (highest safety value first):**
1. Damaged / missing stop signs and yield signs.
2. Broken or unlit streetlights (night-safety value).
3. Faded crosswalk paint / lane markings.
4. Damaged guardrails.
5. Street furniture (benches, bus shelters).
6. Tree health / fallen-branch hazards.

**Mobile-side impact** is bounded — the camera pipeline doesn't care which class fires. Only the server-side asset-type registry and the operator-dashboard categorization meaningfully change.

---

## 5. Cross-cutting concerns

### 5.1 MATLAB fusion engine integration — backend pre-work

Before the MATLAB code arrives, the backend should already have:

1. **Stable I/O contract** for the fusion engine (see §4.3 inputs/outputs).
2. **Raw window blob storage.** Phase 2.0 ingestion endpoint accepts and stores the gzipped `[180, 10]` array. Matching the MATLAB-side window shape avoids a translation layer.
3. **Versioning fields** in `fusion_run` and `fusion_pair` for A/B comparison between heuristic baseline and the MATLAB port.
4. **`fusion_engine_metrics` table** for per-run statistics (TP count, FP estimate, runtime ms, memory peak). Lets us compare engines objectively.

**Anti-patterns to refuse:**
- Calling MATLAB directly from the API layer — couples request latency to MATLAB Runtime startup.
- Embedding fusion math into PL/pgSQL functions — testable in isolation but not portable; locks the engine to one implementation forever.
- Storing fusion outputs in the same row as the source observation — destroys the audit trail when re-running with a new engine.

### 5.2 Multi-asset extensibility — what to commit in Phase 2.0

| Decision | Recommendation | Reason |
|---|---|---|
| Observation table name | `asset_observation`, not `event` | "event" collides with too many other concepts (telemetry, analytics). |
| Discriminator column | `asset_type TEXT`, not `asset_type_id INT` | Human-readable; Postgres indexes text efficiently. |
| Cluster table | `asset_cluster`, not `pothole_cluster` | Same reason. |
| Registry | `asset_type_registry` table | Lookup of asset_type → model_ids, UI category, repair workflow. |
| API path | `/api/v1/observations?type=pothole` (preferred) **plus** `/api/v1/potholes` alias for v1 clients | The alias keeps Phase 1.5/1.6 mobile clients working unchanged for years. |

The alias contract preserves the mobile app: Phase 1.5/1.6 clients calling `GET /api/v1/potholes` get the same response shape, resolved server-side as `/observations?type=pothole`. No mobile code change needed.

### 5.3 Security and compliance

| Concern | Phase 2 MVP | Phase 4.5 enterprise |
|---|---|---|
| Authentication (mobile) | `X-Device-Id` UUID v4 only (current). | Same — anonymous is the value prop. |
| Authentication (dashboard) | Supabase Auth, magic-link, per-municipality role. | OIDC integration with customer IdP (Azure AD, Okta). |
| Authorization | Postgres RLS by municipality. | Same, enforced in API middleware. |
| Data residency | Supabase Canada region. | Customer-specified region or on-prem. |
| Encryption at rest | Supabase default (AES-256). | Customer KMS keys (BYOK). |
| Encryption in transit | TLS 1.3 everywhere. | Same. |
| Audit logging | Application-level: every dashboard action → `audit_log` table. | Same + immutable storage (WORM bucket). |
| PII | None collected (`CLAUDE.md` invariant #3). | None — same. |
| Data retention | 90 d for raw JPEGs, indefinite for observations. | Customer-configurable. |
| Right to delete | `DELETE /api/v1/device/{device_id}` (`roadmap.md` §4.3). | Same, with audit log retained. |

### 5.4 Observability

Three layers, all required:

1. **Mobile crashes** — Sentry (per `roadmap.md` §4.1).
2. **Backend services** — Structured JSON logs → Grafana Loki or Datadog. Metrics → Prometheus / Grafana Cloud. Distributed tracing via OpenTelemetry across API → fusion engine → DB.
3. **Data quality** — A dedicated dashboard counting: observations without paired frames, frames without paired observations, fusion pairs with `delta_ms > 2500`, clusters with `distinct_devices == 1`, ingestion rate anomalies. These are "is the system actually *working*" metrics, distinct from "is it *up*."

### 5.5 Cost model — sanity check, not forecast

Baseline: 1,000 active devices, each producing 50 observations/day + 20 frames/day (~3 MB total) at steady state.

| Item | Supabase | Custom (Cloud Run + Cloud SQL) |
|---|---|---|
| Postgres compute | $25–$100/mo | $50–$150/mo |
| Storage (~100 GB JPEGs) | ~$2/mo | ~$2/mo + $1/mo CDN |
| Bandwidth (tile fetches) | ~$10/mo or free under Pro | ~$10–30/mo |
| Realtime / WebSocket | Included in Pro | $5–20/mo |
| Inference (server-side YOLO, ~20k frames/day) | $40–100/mo (Modal / Replicate) | Same. |
| API compute | Included | $20–50/mo (scale-to-zero) |
| **Total at 1k devices** | **~$80–250/mo** | **~$90–250/mo** |

At small scale, cost difference is noise — velocity dominates.

At 100k devices: Supabase climbs to ~$1k–3k/mo (per-row Realtime quotas, storage egress); custom climbs to ~$500–1.5k/mo with reserved instances. This is where migration pays for itself — but it is also approximately when an enterprise contract makes migration inevitable anyway.

---

## 6. Open questions to resolve in the next turn

1. **MATLAB code share.** Send the methodology (algorithm intent + I/O shapes) so it can be classified per §4.3 buckets (a)/(b)/(c).
2. **Pilot municipality target.** Northeastern-area municipality, GTA municipality, or a direct MTO path? Determines compliance pressure and earliest realistic deployment date.
3. **Dashboard authentication for the pilot.** Magic-link only, or skip directly to OIDC?
4. **Supabase region.** Recommend Canada (data-residency-friendly for MTO conversations). US is lower latency for development but harder to defend politically.
5. **Budget ceiling.** `roadmap.md` §2.11 commits to a $50/mo Supabase cap. Does that hold at projected dashboard traffic, or is the new ceiling $200–500/mo?
6. **Compliance pre-commitment.** Should we begin the SOC 2 / PIPEDA paperwork now (long lead times), or wait for a specific customer to demand it?

---

## 7. Glossary

- **BaaS** — Backend-as-a-Service (Supabase, Firebase, AWS Amplify).
- **MVT** — Mapbox Vector Tiles. Binary protobuf format for vector map data.
- **PMTiles** — Protomaps archive format; a single addressable tile file served from object storage.
- **PostGIS** — Geographic objects extension for PostgreSQL.
- **PostgREST** — Auto-generated REST API from a Postgres schema. Used internally by Supabase.
- **RLS** — Row-Level Security. PostgreSQL feature; per-row access rules expressed in SQL.
- **DBSCAN** — Density-Based Spatial Clustering of Applications with Noise. The clustering algorithm in `ST_ClusterDBSCAN`.
- **MTO** — Ontario Ministry of Transportation.
- **PIPEDA** — Personal Information Protection and Electronic Documents Act (Canada).
- **FedRAMP / CJIS** — US federal cloud certification / US criminal-justice info certification.
- **BYOK** — Bring Your Own Key (customer-managed encryption keys).

---

*End of plan. Companion to [`docs/roadmap.md`](./roadmap.md). The Phase 2.0 schema must be landed **before** the MATLAB port; the fusion-engine interface depends on the asset-observation table layout.*
