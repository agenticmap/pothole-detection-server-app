---
updated: 2026-08-18
---

# Phase 2.5b / 2.5c — Dashboard design, vector basemap, and the operator dock

> Status: **Implemented.** 2.5b shipped the Organic skin, the self-hosted vector basemap, the
> dock and the stats endpoint. 2.5c matched the result to the source design mockup.
>
> Continues [`phase-2.5-dashboard-plan.md`](./phase-2.5-dashboard-plan.md), which remains the
> record for the tiles, the detail panel and the repair write path. Where the two disagree about
> the basemap, this document is current.

## Why

Phase 2.5 shipped a working operator console with a neutral slate-and-blue skin chosen from
first principles. The user then designed a visual identity ("Organic") in Claude Design and
asked for the dashboard to match it.

Doing that surfaced three defects that had nothing to do with styling, and one of them meant the
map had **never worked**. Those are the most valuable part of this record.

---

## 1. The map had never rendered a marker

MapLibre 6 ships its Web Worker as a separate ES module and locates it at runtime:

```js
const file = url.endsWith('-dev.mjs') ? 'maplibre-gl-worker-dev.mjs' : 'maplibre-gl-worker.mjs';
return new URL(`./${file}`, import.meta.url).href;
```

That URL is *computed*, not a literal `new URL('./worker.mjs', import.meta.url)`, so **no bundler
can statically detect it**. Vite emitted no worker chunk and rewrote nothing; the built app asked
for `assets/maplibre-gl-worker.mjs`, got a 404, and the worker never booted.

**The failure is silent and total.** Vector tiles are parsed in the worker, so every vector
source sits in `state: 'loading'` for ever — no error, no console warning, and **zero network
requests for tiles**. A map with a basemap and no markers is indistinguishable from a map with no
data, which is how it survived a whole phase.

Fixed in `dashboard/src/map/worker.ts` + `dashboard/scripts/copy-maplibre-worker.mjs`: the worker
and the shared chunk it imports are copied into `public/maplibre/` on `predev`/`prebuild`, and
`setWorkerUrl()` points at them. `maplibre-gl-shared.mjs` must travel with it — the worker imports
it by relative path.

### The wrong diagnosis, and why it was wrong

This was originally written up as "`addProtocol` does not work for vector tiles in MapLibre 6,
because a protocol registered on the main thread is never consulted by the worker." That claim was
false and is corrected in `phase-2.5-dashboard-plan.md`.

MapLibre's own typings say the opposite — *"This will happen in the main thread, and workers might
call it if they don't know how to handle the protocol"* — and PMTiles is their canonical
`addProtocol` example. The evidence taken as proof (MapLibre's **own** demo vector source hanging
identically) was in fact the tell: *nothing* vector-based worked, because the worker did not exist.

**Lesson worth keeping:** when a vendor's own example fails the same way yours does, that is
evidence about shared infrastructure, not about the API under test.

## 2. The severity ramp was unreachable

`app/sensor_model/features.py` clamps observation severity to `[0, 1]`:

```
severity = clamp(severity_scale * magnitude / max(speed, severity_speed_ref), 0, 1)
```

and a cluster's severity is the median of its members (`app/fusion/service.py`). But
`dashboard/src/severity.ts` had tier floors at **0 / 1.5 / 3 / 5** — above the ceiling. Every real
cluster painted in the palest tier at the smallest radius; three of the four ramp colours could
not occur.

Tier floors are now **0 / 0.25 / 0.5 / 0.75**. These are quartiles of the *possible* range, not
of the observed distribution, and should be recalibrated against real drive data before a pilot.
The endpoint takes them as a **parameter** so `severity.ts` stays the single source of truth (see
§5).

> **Follow-up 2026-08-30.** Fixing the floors was only half of it. `severity_scale` was **2.0**,
> which saturates at `magnitude/max(speed,5) >= 0.5` — below the *minimum* of the observed pothole
> distribution — so once the outlier gate was fixed and real clusters appeared, all but one landed
> in the **top** tier: the same collapse as before, at the other end of the ramp. Scale is now
> **0.25**, from `1/p95` measured over the cluster-admitted potholes, which spreads them 2/12/9/2.
> Treat the floors and the scale as one calibration in two files. See §11.

## 3. Marker radii defeated their own encoding

`circle-radius` was interpolated from 0.7× at z13 to 1× at z18. At the default z14 that made every
marker tiny and collapsed four tiers into near-identical dots — and radius is the redundant channel
that carries severity when colour cannot (CVD, greyscale printing). Radii are now **fixed per
tier** (5 / 7 / 9 / 11, unrated 4), matching the mockup's `circleMarker`.

Selecting a cluster now enlarges it 1.5× and rings it dark (`--color-text`, the inverse of the
halo). It rides `setFeatureState` on the same `promoteId` channel as the optimistic repair flag,
and is re-applied in `addClusterLayers()` because **both `setTiles` and `setStyle` discard feature
state**.

---

## 4. The basemap: self-hosted Protomaps vector tiles

Raster OSM is gone. `app/main.py` mounts a PMTiles archive at `/basemap`; the client registers
`addProtocol('pmtiles', …)` once at module scope in `src/map/basemap.ts`.

Range support is the whole mechanism — Starlette's `FileResponse` implements HTTP `Range`, so a
19 MB archive serves single tiles without a tile server. Without range support a client would pull
the entire file per tile. The mount is deliberately unauthenticated: it is public OSM data, and
the pmtiles protocol owns its own fetch so `transformRequest` never sees it.

Regeneration is documented in [`dashboard/README.md`](../../dashboard/README.md). The archive is a
build artefact under gitignored `storage/`.

### The palette rules, learned by getting them wrong

The design mutes **stock OSM** with a CSS filter:

```css
.leaflet-tile-pane { filter: saturate(0.38) contrast(0.88) brightness(1.08); }
```

That drains OSM's palette while keeping its **hierarchy**: blue-grey water, pale green parks, warm
major roads above white minor ones. Two rules follow, both encoded in the `--map-*` block in
`dashboard/src/styles.css`:

1. **The map is not the chrome.** The first version used `--color-canvas` (cream `#f5ead8`) for the
   land and `--color-surface` for the roads. Those are almost the same value, so the road network
   dissolved into the ground and the pane read as one flat wash continuous with the UI. Cream is
   for the app background; the map uses near-white OSM tones so it reads as a map inside a cream
   frame. **Land must be a step darker than the roads that cut through it.**
2. **`--map-*` values are written out per theme, not derived from `--ramp-neutral-*`.** That ramp
   inverts for dark mode — correct for text and chrome, wrong here, because it would flip "roads
   lighter than land" backwards. The relative ordering matters more than the absolute values, so it
   is stated explicitly twice.

`organicFlavor()` starts from `namedFlavor('light' | 'dark')` and overrides only the palette,
because the named flavour already encodes the road hierarchy (white majors over `#ebebeb` minors,
casings a step darker). It is a *grey* flavour with vivid cyan water (`#80deea`), so land, water
and green space are re-pointed; the structure is Protomaps'.

POI pin layers are filtered out (`layer.id !== 'pois'`). The design reason is primary — restaurant
and school pins compete with the severity markers — and it also silences an upstream gap:
`@protomaps/basemaps` 5.7 references icons (e.g. `townhall`) that the newest published sprite
sheet, v4, does not contain.

### Theme switching

A flavour swap changes dozens of layers, so `applyTheme()` calls `map.setStyle()` rather than
patching paint. `setStyle` discards imperatively-added sources, so the cluster source is rebuilt on
`styledata` — and that rebuild is also what repaints the markers, because `layers.ts` resolves
tokens through `cssVar` at call time and `theme.ts` sets `data-theme` synchronously first.

Optimistic repair feature-state is dropped across the swap. That is deliberate: the next tile fetch
is the source of truth.

### Still outstanding

Glyphs and sprites load from `protomaps.github.io`. Fine for development, **not** for a pilot — a
municipal tool should not lose its street labels because a third-party origin is having a bad day.
Self-hosting them under `dashboard/public/basemap-assets/` is an open task.

---

## 5. `GET /api/v1/clusters/stats`

`app/services/cluster_stats_service.py`, `app/models/stats.py`, route in `app/routes/clusters.py`.
`ViewerOrAbove`, no `Accept-Version` (matching the other dashboard routes). Registered **before**
`/clusters/{cluster_id}` — Starlette matches in registration order and `stats` would otherwise be
read as a cluster id.

```
?bbox=minLon,minLat,maxLon,maxLat   (required)
&asset_type=pothole
&window_days=0                       (0 → cluster_window_days)
&tiers=0,0.25,0.5,0.75               (ascending floors)
```

→ `{ open, repaired, unrated, mean_confidence, repaired_last_30d, tier_counts[],
source_counts{}, generated_at }`

Design notes that matter:

- **Why SQL and not the rendered map.** Below the aggregate zoom the tiles carry only
  `point_count` / `max_severity`, so every KPI would blank out when you zoom out — exactly when a
  city-wide total is most wanted. `tile_buffer` returns the same cluster from adjacent tiles, so
  counting rendered features double-counts along every seam. And `tile_max_features` truncates
  worst-severity-first, silently. SQL has none of those problems and can reach `repair_log`.
- **`tiers` is a parameter, not a server constant.** The ramp is a client concern; a second copy
  here would drift, and the first symptom would be a legend disagreeing with its own map.
- **`source_counts` omits absent sources rather than reporting zero.** "No camera-reviewed
  clusters here" and "camera review contributed 0" are different claims and only the first is true.
- `mean_confidence` is `null`, not `0.0`, when nothing is open — an average of nothing is not zero
  confidence, and the card renders `—`.
- The bbox is transformed into 3857 the way the tile query does, so it hits
  `idx_asset_cluster_centroid_3857` from `migrations/007_tiles.sql`. Verified: index scan, ~0.3 ms.

Reuses `_parse_bbox` from `app/routes/potholes.py`. Tests in `tests/test_cluster_stats.py` (27):
exact counts, tier bucketing *at* the boundaries, source-count behaviour, 401/403, and malformed
input.

---

## 6. The design source of truth, and the token bridge

The mockup is committed at repo root as **`RoadWatch Dashboard.dc.html`** (a Leaflet prototype).
If it goes missing from the working tree, recover it with:

```bash
git show "HEAD:RoadWatch Dashboard.dc.html"
```

It is the authority for layout, ordering and strings. `handoff/INTEGRATION.md` in the Claude Design
project is the integration spec, but note it is *not* always right about intent — its §7 said to
"desaturate in the style JSON" without saying the target was *muted OSM* rather than monochrome
cream, which is how the flat-wash basemap happened.

**The mockup uses design-system token names the repo does not have.** `dashboard/src/tokens.css` is
a faithful rename of them, confirmed by the mockup's own dark-mode block hard-coding the same hexes
(`--color-neutral-100: #403b32`, `--color-divider: color-mix(in srgb, #f9f4ed 18%, transparent)`).
Use this map when translating any value out of the mockup:

| Mockup (design system) | Repo |
|---|---|
| `--color-bg` | `--color-canvas` |
| `--color-divider` | `--color-border` |
| `--color-accent` | `--color-primary` |
| `--color-accent-N` | `--ramp-accent-N` |
| `--color-accent-2` / `--color-accent-2-N` | `--color-accent-2` / `--ramp-sage-N` |
| `--color-neutral-N` | `--ramp-neutral-N` |
| `--font-heading` / `--font-body` | `--font-display` / `--font-ui` |

`--color-surface` and `--color-text` are unchanged. Accessing the Claude Design project again needs
`/design-login` first; the API gate closes (`permission_denied: access gate closed`).

### Two override-layer traps

`dashboard/src/organic-shell.css` is a user-supplied override layer that loads **last**, so it wins
on equal specificity. Two consequences already hit:

- `.section-title` is styled there as an uppercase letterspaced terracotta eyebrow. That is right
  for the legend's "Severity" and wrong for the panel's section headings, which the mockup renders
  as display-font headings. The panel therefore uses its own `.panel-section-title` rather than
  fighting the override.
- `[hidden]` sets `display: none` in the UA stylesheet, which any class rule with its own `display`
  beats. `.dock-collapsed` is `display: flex`, so the collapsed pill rendered *on top of* the open
  dock until `[hidden]` was restated at class specificity.

---

## 7. What is deliberately not backed by data

The mockup shows figures this schema cannot produce. They render in their designed position — the
layout is the design's — but never as a fabricated number, because a city may act on this screen.
`provisional()` in `dashboard/src/dock.ts` marks them all the same way so they stay greppable.

| Mockup element | What ships | Why |
|---|---|---|
| KPI delta lines ("−214 this month") | `—` with the reason on hover | No baseline exists: `asset_cluster.updated_at` is rewritten by every clustering pass |
| "Find a street" | disabled input, "needs a street column" | No street column anywhere in the schema |
| Panel street heading | the cluster's coordinates | Real data; an invented street name on a dispatch screen is the wrong kind of fidelity |
| Detection-source chips | real `source_counts`, in practice one `crowd · N` chip | Backed, but the clustering job only ever writes `'crowd'` — that *is* the truth about the data |
| "Cluster promoted" / "Verified on site" history | absent | `repair_log.action`'s CHECK permits only `repaired` / `unrepaired` |

---

## 8. Demo data

`scripts/seed_demo.py` — 120 deterministic synthetic clusters over Toronto with observations,
camera frames (real JPEGs) and repair history, shaped to exercise every visual path. Guarded to
`pothole_test` / `pothole_ci`, mirroring `tests/conftest.py`.

Two schema traps it handles, both of which otherwise produce a silently empty map:

- **`last_seen` must be non-NULL and inside `cluster_window_days`.** Every tile query filters
  `last_seen >= now() - window`, and `NULL >= x` is NULL.
- **Frames are reached through `fusion_pair`, not `asset_frame.event_client_id`.** Each frame needs
  all five rows plus a file on disk; `tests/test_cluster_detail.py` exists to prove a frame linked
  only by that column never appears.

`pytest` TRUNCATEs every table, so a test run wipes the seed *and* the staff accounts. Accounts and
the re-seed procedure are in [`dashboard/README.md`](../../dashboard/README.md).

---

## 9. Verification

- `pytest -q` → **233 passed**. `ruff check app/ tests/ scripts/` → the three long-standing nits.
- `npm run build` clean. Bundle **269.9 KB JS + 15.5 KB CSS** gzipped, against the 800 KB budget in
  the architecture doc §3.4. `pmtiles` + `@protomaps/basemaps` cost ~18 KB gzipped.
- **The map is the acceptance test.** In light mode: water reads blue-grey, major roads are
  distinguishable from minor, parks are pale green, and the markers are the most saturated thing on
  screen. Verified with Playwright against the user's own side-by-side screenshots.
- **Dark mode is the strict one**: toggle and confirm the markers repaint *and* the cluster source
  survives `setStyle`. Screenshotting the chrome alone hides both failures.
- At the default z14 all four tier radii must be visibly different, and selecting a cluster must
  enlarge and dark-ring it.

---

## 10. Known gaps and next steps

- **Org display name.** The dock heading shows `org_id` (`org_demo`) where the mockup shows "City of
  Toronto". `org.name` exists in the database but neither the JWT nor the login response carries it
  — one additive field on `TokenResponse`.
- **Street names — the largest missing capability.** It blocks the panel heading, "Find a street",
  and street names in any report or work order. The recommended route is **not** a geocoding API
  but importing the municipality's own road centreline:
  - Load the city's centreline GeoJSON (Toronto's has `LINEAR_NAME_FULL`, `CENTRELINE_ID`) into a
    generic `road_centreline(centreline_id, name, geom GEOGRAPHY(LINESTRING))` with a GiST index;
    add `street` / `centreline_id` to `asset_cluster`; snap in the clustering job with a
    nearest-neighbour query and a distance cap.
  - Why centreline over an API: no key, no rate limit, no per-request cost, no third party in the
    request path, works offline, and it is the city's authoritative data — which matters when a work
    order cites a location.
  - Caveats: it is **per-city** (different field names each time, so keep the table generic);
    snapping is approximate, so store the snap distance and withhold a low-confidence name rather
    than assert it; and centreline gives the street but **not** the house number the mockup shows
    ("Spadina Ave · 195") — that needs the separate address-point dataset.
  - A cheaper client-side alternative exists and was rejected: road names are already in the PMTiles
    `roads` layer, so `queryRenderedFeatures()` could fill the panel heading with no server work.
    It cannot back a server-side search or be stored, so it would be built twice.
  - **The road GeoJSON is not useful for *drawing* roads.** The PMTiles archive already has the full
    OSM network, tiled, named and classified, plus water/parks/buildings. A GeoJSON would be one
    un-tiled blob, one city only, without the rest of the basemap.
- **KPI deltas** need a snapshot or time-series table before they can show a real figure.
- **Self-hosted glyphs/sprites** — see §4.
- Still open from Phase 2.5: **no automated frontend tests**. (`POST /auth/logout` and the
  dashboard bundle in the Docker image were listed here too; both shipped in Phase 2.6, §8 and §5.)

## 11. Amendment 2026-08-30 — raw observations, and the legend was covering the controls

Two changes from the [integration round](./integration-round-2026-08.md).

**A raw-observation layer** (`SOURCE_OBSERVATIONS` in `map/layers.ts`). `GET /api/v1/tiles/observations`
had existed and been tested since Phase 2.5 but was never wired into the frontend, so individual
sensor readings appeared at no zoom on any surface. That mattered more than it sounds: only 110 of
166 cluster-admitted observations land in a cluster, so a third of what the sensor reported was
unreachable from the console. The layer is off by default behind a dock toggle, sits beneath the
clusters, and draws **outlier-rejected readings hollow** — those are the rows the cluster member
gate silently drops, and hiding them would make that gate unfalsifiable from the UI. Clicking one
opens a popup with every attribute the tile carries, including unknown keys, so a new column in
`_OBSERVATION_TILE_SQL` shows up without a frontend change.

Two traps worth recording. The source needs its **own** `maxzoom`: written as
`minzoom: 15, maxzoom: SOURCE_MAX_ZOOM` (14) the range is empty and MapLibre silently fetches
nothing, which presents exactly like "no data". And `minzoom` is load-bearing rather than cosmetic —
the endpoint 400s below `TILE_OBSERVATIONS_MIN_ZOOM` and an errored MapLibre tile never retries, so
one request at z14 leaves a permanently dead tile.

**The legend covered MapLibre's bottom-right controls.** At `bottom: var(--space-3)` it spanned
y 653–865 against the zoom buttons at 765–823 and the attribution at 843–867; Playwright reported
`.legend intercepts pointer events` when asked to click zoom-in. The attribution half is not
cosmetic — the OSM/Protomaps basemap licence requires it to stay visible. `shell.ts` now measures the
control stack and publishes `--map-ctrl-bottom-h`; the legend clears it with `calc()`. Measured
rather than hardcoded because the attribution re-wraps on a narrow pane and the compact/expanded
toggle changes its height at runtime.

**Also corrected here:** this file's header described `severity_scale = 2.0`. It is now 0.25 — the
old value saturated below the *minimum* of the observed pothole distribution, so every cluster
painted in the top tier. `dashboard/src/severity.ts` had already recorded the mirror-image failure
(floors above the ceiling, everything in tier 1). The tier floors and `SEVERITY_SCALE` are one
calibration split across two files; changing either alone re-breaks the ramp.
