---
updated: 2026-08-17
---

# RoadWatch — operator dashboard

The municipal operator console for the pothole-detection server (Phase 2.5). Map-first: sign
in, see confirmed potholes, open one, look at the camera frames behind it, mark it repaired.

Design rationale, the decisions behind the odd-looking bits, and what is deliberately not
built live in [`../docs/phase-2.5-dashboard-plan.md`](../docs/phase-2.5-dashboard-plan.md).
**Read that before changing the tile auth or the severity encoding** — both have non-obvious
constraints that cost real debugging time to find.

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
  map/
    map.ts         MapLibre init, source, clicks, optimistic repaint
    tile-auth.ts   transformRequest + 401 recovery
    layers.ts      the two style layers
    basemap.ts     raster OSM style — swap here for PMTiles
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
- **`addProtocol` does not work for vector tiles.** MapLibre 6 loads them in a Web Worker and
  never consults a main-thread protocol; tiles hang in `state: 'loading'` with no error and no
  network request. Use `transformRequest` — see `map/tile-auth.ts`.
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

Filters, the inventory list, work orders, reports, dark mode, and any automated frontend
tests. The module rail has disabled slots for the first four so adding them is additive.
Raster OSM basemap tiles are dev-only under OSM's usage policy.
