---
updated: 2026-08-23
---

# RoadWatch — operator dashboard

The municipal operator console for the pothole-detection server (Phase 2.5). Map-first: sign
in, see confirmed potholes, open one, look at the camera frames behind it, mark it repaired.

Design rationale, the decisions behind the odd-looking bits, and what is deliberately not
built live in [`../docs/phase-2.5-dashboard-plan.md`](../docs/phase-2.5-dashboard-plan.md) and
its successor [`../docs/phase-2.5b-dashboard-design.md`](../docs/phase-2.5b-dashboard-design.md)
(the Organic redesign, the vector basemap, the dock).

**Read those before changing the tile auth, the basemap palette or the severity encoding** — all
three have non-obvious constraints that cost real debugging time to find.

## Quick start

```bash
npm install

# Production-ish: build, then let FastAPI serve it at /dashboard
npm run build
cd .. && uvicorn app.main:app --reload --port 8000
#   → http://127.0.0.1:8000/dashboard/

# Development: HMR, with /api proxied so dev is same-origin too
VITE_API_TARGET=http://127.0.0.1:8000 npm run dev
#   → http://localhost:5173/dashboard/
```

You need a staff account. There is no self-signup:

```bash
cd .. && POTHOLE_STAFF_PASSWORD='…' python scripts/create_staff.py \
    --org org_cambridge --name "City of Cambridge" \
    --email jane@cambridge.gov --role staff
```

`viewer` sees everything but no repair control; `staff` and `admin` can mark repairs.

For a throwaway account against the test database — with 120 synthetic clusters to look at —
use the ready-made one under [Demo data](#demo-data) instead.

### Set `AUTH_JWT_PRIVATE_KEY_PEM` before you start

Without it the server mints an **ephemeral signing key per process**, so every `--reload` logs
you out — and under `--workers 2` each worker signs with a different key, producing
intermittent 401s that look like a client bug. `.env.example` has the generation command.

## Scripts

| Command | Does |
|---|---|
| `npm run dev` | Vite dev server on :5173 with `/api` + `/health` proxied |
| `npm run build` | `tsc --noEmit` then build to `dist/` |
| `npm run typecheck` | Types only |
| `npm run preview` | Serve the built bundle |

## Environment

All optional, all build-time (`VITE_`-prefixed, so they are **baked into the bundle** — never
put a secret here):

| Variable | Default | Purpose |
|---|---|---|
| `VITE_MAP_LAT` / `VITE_MAP_LON` | Toronto | Initial map centre |
| `VITE_MAP_ZOOM` | `14` | Initial zoom — keep it **above 12** or the first thing an operator sees is aggregate bubbles, which are not clickable |
| `VITE_API_TARGET` | `http://127.0.0.1:8000` | Dev-server proxy target only |

There is no extent endpoint, so the starting view is configured rather than discovered.

## Layout

```
src/
  main.ts          bootstrap; login ↔ shell; WebGL2 gate
  auth.ts          token lifecycle, single-flight refresh, role claim
  api.ts           authenticated fetch; 401 → refresh → retry once
  types.ts         mirrors of app/models/clusters.py — keep field names exact
  dom.ts           el() / field() / plural(); textContent only, no innerHTML
  severity.ts      the ordinal ramp — colour + radius + label, one source of truth
  shell.ts         top bar, asset selector, module rail, legend, URL state
  dock.ts          KPI + filter dock; owns the "unbacked value" policy
  stats.ts         /clusters/stats client — every count on screen comes from here
  tokens.ts        cssVar(), the one way JS reads a design token
  map/
    map.ts         MapLibre init, source, clicks, optimistic repaint
    tile-auth.ts   transformRequest + 401 recovery
    layers.ts      the two style layers
    basemap.ts     Protomaps vector style; the Organic flavour reads --map-* tokens
    worker.ts      setWorkerUrl — READ THIS before debugging an empty map
  panel/
    panel.ts       detail panel; one AbortController per open
    frames.ts      blob-URL images, concurrency-capped
  tokens.css       semantic design tokens
  styles.css       shell layout + components
```

## Things that will bite you

Each of these cost real time to find. They are in the code as comments too.

- **`<img src>` cannot send an `Authorization` header.** Frame images must be `fetch`ed and
  turned into blob URLs. Do not "simplify" `panel/frames.ts` back to a plain `src`.
- **MapLibre's worker is a static asset, not a bundled chunk — and if it 404s, EVERY vector
  source silently never loads.** No error, no console warning, no tile requests, just an empty
  map. `npm run dev` and `npm run build` both run `scripts/copy-maplibre-worker.mjs` first for
  this reason. Check the Network panel for the worker before suspecting auth, tiles or SQL. See
  `map/worker.ts`.
  - This corrects an earlier entry here which claimed `addProtocol` "does not work for vector
    tiles" because the worker never consults the main thread. **That was wrong** — the worker was
    404ing, so nothing vector-based worked at all. `addProtocol` does work, and the PMTiles
    basemap depends on it.
- **`transformRequest`, not `addProtocol`, for the *authenticated* cluster tiles** — see
  `map/tile-auth.ts`. Not because the protocol hook fails, but because the access token lives on
  the main thread where the refresh logic is.
- **An errored tile never retries itself.** A 401 leaves the map permanently blank unless
  something forces a refetch. Hence the explicit `map.on('error')` → refresh → `setTiles`.
- **`ST_AsMVT` omits NULL attributes entirely**, and `severity` is nullable — so every numeric
  `['get', …]` must be wrapped in `coalesce` or MapLibre throws a render-time expression error.
- **Source `maxzoom` must stay above 12.** At 12 or below, aggregated tiles get overzoomed
  forever and individual clusters never appear at any zoom.
- **`setTiles` discards feature state**, so the optimistic repair repaint and a tile reload are
  alternatives, not a pair.
- **MapLibre puts `.maplibregl-map` on the container you give it**, not on a child — so do not
  absolutely position that class.
- **`Number(null)` is `0`.** Parse URL params through a helper that distinguishes absent from
  zero, or a first-time visitor lands at (0, 0).
- **Never use `innerHTML`.** The repair note is operator free text the API echoes back.

## Not built

The inventory list, work orders, reports, and any automated frontend tests. The module rail has
disabled slots for the first three so adding them is additive.

Filters and dark mode **were** built in Phase 2.5b; the basemap is no longer raster OSM. See
[`docs/phase-2.5b-dashboard-design.md`](../docs/phase-2.5b-dashboard-design.md) for the as-built
record, including what the dashboard deliberately does *not* claim to know (street names, KPI
deltas) and why.

---

## Phase 2.5b — vector basemap and demo data

### The basemap archive

The map background is a **self-hosted Protomaps PMTiles archive**: one file, served over HTTP
range requests, with no basemap tile server. FastAPI mounts it at `/basemap` (`app/main.py`),
and Starlette's `FileResponse` implements `Range`, which is the whole mechanism — without range
support a client would pull the entire archive to read one tile.

It is a build artefact, not source. `storage/` is gitignored, so regenerate it after a clone:

```bash
# 1. Get the CLI — one Go binary, from github.com/protomaps/go-pmtiles/releases
# 2. Cut a bbox out of the daily planet build. Only the needed byte ranges are
#    downloaded, so this is fast (Toronto at z14 is ~19 MB in a few seconds).
pmtiles extract https://build.protomaps.com/<YYYYMMDD>.pmtiles \
  storage/basemap/toronto.pmtiles --bbox=-79.64,43.58,-79.11,43.86 --maxzoom=14
```

Builds are listed at `maps.protomaps.com/builds` and are retained for about two weeks, so pick
a recent date. Verify the result by dropping it on <https://protomaps.github.io/PMTiles/>.
`VITE_BASEMAP_URL` overrides the path if you cut a different region.

`--maxzoom=14` matches `SOURCE_MAX_ZOOM` in `src/map/map.ts`; MapLibre overzooms above it.

**Glyphs and sprites are still fetched from Protomaps' GitHub Pages origin.** That is fine for
development and *not* fine for a pilot — a municipal tool should not lose its street labels
because a third-party origin is having a bad day. Self-hosting them under
`public/basemap-assets/` is an open task.

### Demo data

Order matters for the first two — the seed attaches its repair history to whichever staff
account already exists, so creating the account first is what makes the detail panel show an
operator's email rather than a raw `usr_…` id.

```bash
export DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_test

# 1. The demo accounts — an admin and a viewer, so both roles can be exercised
POTHOLE_STAFF_PASSWORD='roadwatch-demo' python scripts/create_staff.py \
    --org org_demo --name "RoadWatch Demo" \
    --email ops@example.com --role admin --full-name "Demo Operator"

POTHOLE_STAFF_PASSWORD='roadwatch-viewer' python scripts/create_staff.py \
    --org org_demo --name "RoadWatch Demo" \
    --email viewer@example.com --role viewer --full-name "Demo Viewer"

# 2. The clusters
python scripts/seed_demo.py --reset

# 3. Serve it. The dev container on :8000 points at pothole_db, which is real data.
uvicorn app.main:app --port 8010
#   → http://localhost:8010/dashboard/
```

**Demo credentials** — `pothole_test`, org `org_demo`

| Email | Password | Role | Use it for |
|---|---|---|---|
| `ops@example.com` | `roadwatch-demo` | `admin` | The full console, including marking repairs |
| `viewer@example.com` | `roadwatch-viewer` | `viewer` | Checking the read-only tier |

The two roles genuinely differ, and not just in the UI: a `viewer` token reads
`/clusters/stats` fine (200) and is refused on `POST /clusters/{id}/repair` (**403**) — verified
against the running server. The dashboard also hides the repair control, but that is a hint, not
the enforcement; the server re-reads `org_member` on every write.

> **Since Phase 2.6 a repair can 403 for a second, unrelated reason: ownership.**
> `asset_cluster.org_id` scopes writes — another org's cluster is refused even for an `admin`,
> and an **unowned** cluster (`org_id IS NULL`) takes an `admin`. Because the clustering job
> sets no `org_id`, *everything the pipeline produces is unowned*, so a plain `staff` operator
> currently cannot mark real detections repaired. Provision operators who need to as `admin`
> until the job learns to assign an owner. Full reasoning:
> [`../docs/phase-2.6-hardening.md`](../docs/phase-2.6-hardening.md) §6.
>
> So when debugging a 403 on repair, check the cluster's `org_id` before assuming it is the role.

These are **throwaway local-only credentials for a database that every test run wipes**. They
are written down because the alternative is re-deriving them each time, and because
`@example.com` cannot receive mail and `pothole_test` holds nothing real. Do not reuse these
passwords elsewhere, and do not provision these accounts against `pothole_db` or a deployed
server — `scripts/create_staff.py` creates them wherever `DATABASE_URL` happens to point.

120 deterministic synthetic clusters over Toronto, with observations, camera frames (real JPEGs
on disk), and repair history. `seed_demo.py` refuses to run against any database but
`pothole_test` / `pothole_ci`, mirroring the guard in `tests/conftest.py`.

Note that **`pytest` TRUNCATEs every table**, so a test run wipes the seed *and* the staff
account. Re-run steps 1 and 2 afterwards.


### Accounts already in the dev database

`pothole_db` — the database the compose stack on `:8000` uses, and the one holding the real
collected drive data. These were provisioned during earlier phases:

| Email | Role | Org | Password | Can sign in? |
|---|---|---|---|---|
| `ops@example.com` | `admin` | `org_test` | **not recoverable** | yes |
| `viewer@example.com` | `viewer` | `org_test` | **not recoverable** | yes |
| `ops@test.local` | `admin` | `org_test` | **not recoverable** | **no — see below** |
| `demo@roadwatch.dev` | `admin` | `org_test` | `roadwatch-demo` | yes |

> `demo@roadwatch.dev` was provisioned against `pothole_db` on 2026-08-23 to demo the console,
> because none of the passwords above are recoverable. That is **against the advice two sections
> up** ("do not provision these accounts against `pothole_db`"), and it is recorded here rather
> than left as an undocumented account with a published password. It is `admin` because
> unowned clusters are admin-only to repair (see the Phase 2.6 note above). Remove it with:
>
> ```bash
> docker compose exec -T postgres psql -U pothole -d pothole_db >   -c "DELETE FROM staff_user WHERE email = 'demo@roadwatch.dev';"
> ```
>
> `org_member` and `refresh_token` cascade from `staff_user`.

**The passwords cannot be documented, because they cannot be read.** `staff_user.password_hash`
is bcrypt, which is one-way by design — that is the property that makes the column safe to hold.
Nothing in the repo, the logs, or the database can turn those hashes back into text. If you know
them, they are worth writing into your own password manager rather than here; if you do not,
reset one:

```bash
DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_db python - <<'PY'
import asyncio, sys; sys.path.insert(0, ".")
import asyncpg
from app.auth.passwords import hash_password
from app.config import settings

EMAIL, NEW_PASSWORD = "ops@example.com", "put-a-new-one-here"

async def main():
    conn = await asyncpg.connect(settings.database_url)
    # lower(email) to match idx_staff_user_email_lower, the same way login does.
    print(await conn.execute(
        "UPDATE staff_user SET password_hash = $1 WHERE lower(email) = lower($2)",
        hash_password(NEW_PASSWORD), EMAIL))
    await conn.close()

asyncio.run(main())
PY
```

There is no `--reset-password` flag on `scripts/create_staff.py` and no password-change endpoint;
that gap is recorded under [Production accounts](#production-accounts).

**`ops@test.local` can never log in, whatever its password is.** The login route validates with
Pydantic's `EmailStr`, which rejects the reserved `.local` TLD, so the request 422s before any
credential check. It predates the fix that made `create_staff.py` validate the same way
(`docs/phase-2.5-dashboard-plan.md`), and it is safe to delete:

```sql
DELETE FROM org_member WHERE user_id = (SELECT user_id FROM staff_user WHERE email = 'ops@test.local');
DELETE FROM staff_user WHERE email = 'ops@test.local';
```

Two things worth knowing about this set:

- **`ops@example.com` exists in both databases with different passwords** — `roadwatch-demo` in
  `pothole_test`, something else in `pothole_db`. Same address, different account, different
  org (`org_demo` vs `org_test`). Check which database your server is pointed at before
  concluding a password is wrong.
- **These are not org-scoped in a way that limits them.** `asset_cluster` has no `org_id`, so an
  `org_test` admin can repair anything; see the gap noted under Production accounts.

### Production accounts

**There is no production username and password, and there must never be one written down
here.** Two separate reasons:

1. **Nothing is deployed yet.** Production hardening is Phase 4 (`docs/roadmap.md`), still
   planned. The only deployment artefacts in the repo are the `Dockerfile` and a local
   `docker-compose.yml`.
2. **A shared production credential is the wrong shape anyway.** `repair_log.user_id` is an
   audit trail — it records who marked a defect repaired, and a city may have to stand behind
   that record. One shared login makes every entry say the same thing and the audit worthless.
   Provision **one account per operator**.

And unlike the demo login above, a production credential committed here could not be withdrawn:
`git rm` does not remove anything from history, and this repo has a remote.

#### Provisioning an operator, once there is a deployment

Same script, pointed at the deployed database. The password is read from the environment or
prompted for — never from `argv`, so it stays out of shell history and the process list.

```bash
DATABASE_URL='<deployed connection string>' \
  python scripts/create_staff.py --org org_cambridge --name "City of Cambridge" \
    --email jane@cambridge.gov --role staff
# Password: ‹prompted; not echoed›
```

Rules that follow from the above:

- Let it **prompt**. Use `POTHOLE_STAFF_PASSWORD` only in an interactive shell you control, and
  never in a script, a CI job, or a file that gets committed.
- The operator should choose their own password, or be sent a generated one **out of band**
  (not in the same channel as the URL). There is no password-reset flow to recover from a bad
  hand-off — see the gaps below.
- `viewer` for read-only staff; `staff` or `admin` only for people who should be able to mark
  repairs. `admin` is not required for day-to-day operation.
- The connection string is itself a secret. It belongs in the host's secret store, not in a
  committed `.env`. `.env.example` deliberately ships every secret value empty.

#### Pre-flight, or auth breaks in ways that look like client bugs

- **`AUTH_JWT_PRIVATE_KEY_PEM` must be set.** Left empty the server mints an ephemeral keypair
  per process, and `Dockerfile:30` runs `uvicorn --workers 2` — so each worker signs with a
  different key and tokens minted by one fail on the other. That surfaces as *intermittent*
  401s. Outside `ENV=development` it fails closed (`RuntimeError` in `app/auth/keys.py:64`),
  which is the correct behaviour; do not work around it by setting `ENV=development` in a
  deployment. Generation command is in `.env.example`.
- **`ENV=production` means migrations do not run on boot** (`app/main.py` only migrates in
  development). Apply them as a deliberate deployment step.
- **`DATABASE_USE_POOLER=true`** when going through Supabase's transaction pooler, or asyncpg's
  statement cache produces "prepared statement does not exist" errors under load.

#### Lifecycle — what actually exists today

| Operation | How | Status |
|---|---|---|
| Disable an account | `UPDATE staff_user SET disabled = TRUE WHERE lower(email) = lower('…')` | Works — checked on every login (`app/auth/service.py`) |
| Change a role | `UPDATE org_member SET role = '…'` | Works for writes immediately (they re-read `org_member`); reads use the role baked into the token until it expires (≤ 30 min) |
| Rotate a password | SQL `UPDATE staff_user SET password_hash = …` with a fresh bcrypt hash | **No endpoint or script.** Gap |
| Revoke a session | Delete the user's `refresh_token` rows | Access token still works until it expires (≤ 30 min) |

**Gaps to close before a real pilot**, none of which are hidden by the table above: no
password-change or password-reset flow, no `POST /auth/logout`, no MFA, and **no throttling on
the login endpoint** — the rate limiter is device-keyed and ingest-only, so `/auth/login` is
unprotected against credential stuffing. Also note the authorization gap already recorded in
`app/routes/clusters.py`: `asset_cluster` has no `org_id`, so any staff member of any org can
repair any city's clusters.

### Things that will bite you (continued)

- **MapLibre's worker is a static asset, not a bundled chunk.** MapLibre 6 computes its worker
  URL at runtime, so no bundler emits it; `npm run dev` and `npm run build` both run
  `scripts/copy-maplibre-worker.mjs` first to place it under `public/maplibre/`. If that script
  is skipped the worker 404s and **every vector source silently never loads** — no error, no
  console warning, no tile requests, just an empty map. See `src/map/worker.ts`.
- **Severity is on a [0, 1] scale**, not the single digits the tier comment used to claim. The
  server clamps it (`app/sensor_model/features.py`); the tier floors are 0 / 0.25 / 0.5 / 0.75.
- **A theme change swaps the whole style**, so the cluster source is rebuilt and any optimistic
  repair feature-state is dropped. That is deliberate; the next tile fetch is the truth.
