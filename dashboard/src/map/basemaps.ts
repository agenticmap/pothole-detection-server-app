/**
 * Which map is under the markers.
 *
 * Split out of basemap.ts, and the split is by DEPENDENCY rather than by topic:
 * nothing in this file may import `maplibre-gl`. That library touches `window` at
 * import time, and the vitest suite runs in the `node` environment (there is no
 * `test` block in vite.config.ts and no vitest.config.*), so a spec that reached
 * `maplibre-gl` transitively could not even be collected. Keeping the registry,
 * the flavor names and the Esri source spec here is what makes them testable;
 * basemap.ts keeps everything impure — the pmtiles `addProtocol`, the glyph and
 * sprite URLs, and `organicFlavor`, which reads CSS custom properties.
 */

import { namedFlavor, type Flavor } from '@protomaps/basemaps';
import type { RasterSourceSpecification } from '@maplibre/maplibre-gl-style-spec';

/**
 * `organic` is the house style and the default: a warm, drained flavor built from
 * the --map-* tokens so the severity markers stay the only saturated thing on
 * screen (see basemap.ts). The five named flavors are Protomaps' own, offered
 * unmodified — an operator cross-checking against another tool, or printing a work
 * order, wants a plain map. `satellite` answers a different question altogether:
 * is the thing the sensor hit actually a pothole, or a manhole cover?
 */
export type BasemapId =
  | 'organic'
  | 'light'
  | 'dark'
  | 'white'
  | 'grayscale'
  | 'black'
  | 'satellite';

export interface BasemapOption {
  id: BasemapId;
  label: string;
  /**
   * The argument to pass `namedFlavor()`, or null for the two options that are not
   * a named flavor. There are exactly FIVE names — light, dark, white, grayscale,
   * black — and anything else throws `Flavor not found` at the moment the operator
   * picks it. In particular the spelling is `grayscale`; `grey` and `gray` both
   * throw. basemaps.spec.ts asserts every id in this table against the library.
   */
  flavor: string | null;
  /**
   * True only for `organic`, whose palette is derived from the live `data-theme`.
   *
   * This does NOT mean a theme flip is a no-op for the others — see
   * PotholeMap.applyTheme, which re-styles regardless. It only records which
   * basemaps change appearance when the chrome does.
   */
  themed: boolean;
}

export const DEFAULT_BASEMAP: BasemapId = 'organic';

export const BASEMAPS: readonly BasemapOption[] = [
  { id: 'organic', label: 'Organic', flavor: null, themed: true },
  { id: 'light', label: 'Light', flavor: 'light', themed: false },
  { id: 'dark', label: 'Dark', flavor: 'dark', themed: false },
  { id: 'white', label: 'White', flavor: 'white', themed: false },
  // Label in the house register, id as the library spells it.
  { id: 'grayscale', label: 'Greyscale', flavor: 'grayscale', themed: false },
  { id: 'black', label: 'Black', flavor: 'black', themed: false },
  { id: 'satellite', label: 'Satellite', flavor: null, themed: false },
];

export function isBasemapId(value: unknown): value is BasemapId {
  return typeof value === 'string' && BASEMAPS.some((option) => option.id === value);
}

export function basemapOption(id: BasemapId): BasemapOption {
  // The registry is exhaustive over BasemapId, so this cannot miss for a typed
  // caller; the fallback covers a value that arrived as a bare string.
  return BASEMAPS.find((option) => option.id === id) ?? BASEMAPS[0]!;
}

export const SATELLITE_SOURCE_ID = 'esri-imagery';

/**
 * Esri World Imagery.
 *
 * **A third-party runtime dependency with no contract behind it** — the same
 * category as the protomaps.github.io glyphs and sprites basemap.ts warns about,
 * but in the hot path for every tile rather than once per session. There is no key
 * and no published rate limit. A municipal pilot should move to a purchased ArcGIS
 * subscription or the province's orthophoto WMTS before this reaches anyone paying
 * for it; swapping it is this one object.
 *
 * `maxzoom: 19` is not caution. Outside its coverage Esri answers **HTTP 200 with a
 * small "no data" placeholder JPEG**, not a 404 — so MapLibre's error path never
 * fires and the operator silently gets grey squares indistinguishable from a real
 * parking lot. Capped, MapLibre overzooms a genuine tile instead.
 *
 * The path is ArcGIS REST's `/{level}/{row}/{col}`. MapLibre substitutes by name,
 * so what matters is the ORDER: `/{z}/{y}/{x}`, never `/{z}/{x}/{y}`. Transposed
 * imagery is the failure mode if this is copied carelessly, and it looks like a
 * network fault rather than a typo.
 *
 * No bearer token is attached to these requests: transformRequest matches on the
 * `/api/v1/` PATHNAME rather than the origin (tile-auth.ts), which is load-bearing
 * here and is asserted in basemaps.spec.ts.
 */
export function satelliteSource(): RasterSourceSpecification {
  return {
    type: 'raster',
    tiles: [
      'https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    ],
    tileSize: 256,
    minzoom: 0,
    maxzoom: 19,
    attribution:
      'Imagery © Esri — Esri, Maxar, Earthstar Geographics, and the GIS User Community',
  };
}

/**
 * Label colours for imagery.
 *
 * Deliberately NOT the --map-* tokens and not organicFlavor: those halos are
 * --map-land (a pale cream) under muted warm-brown text, which is tuned for a
 * drained ground and is unreadable over a photograph of a city.
 * `namedFlavor('black')` is the closest base — it already assumes a dark ground —
 * with the label keys pushed to near-white on a solid dark halo. That pair is the
 * one that survives a bright parking lot and a shadowed tree canopy in the same
 * tile.
 *
 * Literal values rather than tokens, on purpose: these are relative to the
 * IMAGERY, not to the UI theme. Aerial photography does not get darker in dark
 * mode. (The marker halo is the opposite case and does move — see the
 * `[data-basemap='satellite']` block in tokens.css.)
 */
export function satelliteFlavor(): Flavor {
  const text = '#f2ede4';
  const halo = '#12100d';
  return {
    ...namedFlavor('black'),
    roads_label_minor: text,
    roads_label_minor_halo: halo,
    roads_label_major: text,
    roads_label_major_halo: halo,
    subplace_label: text,
    subplace_label_halo: halo,
    city_label: text,
    city_label_halo: halo,
    state_label: text,
    state_label_halo: halo,
    ocean_label: text,
  };
}

/** Backdrop under the imagery, so the first frame is not a flash of page ground. */
export const SATELLITE_BACKDROP = '#12100d';

/**
 * The operator's basemap preference.
 *
 * Mirrors theme.ts exactly, for the same reasons: a module-level `current`, an
 * `init` called once before the first render, localStorage wrapped in try/catch
 * for private browsing, and the stored value validated on read so a hand-edited
 * entry falls back rather than throwing `Flavor not found` and blanking the map.
 *
 * NOT in the URL hash. The hash exists so an operator can send a colleague a link
 * to a specific defect (shell.ts); which basemap they happen to like is a display
 * preference like the theme, and `writeUrlState` rebuilds the query from scratch,
 * so every extra key is one more thing `sync()` has to thread through.
 */
const STORAGE_KEY = 'roadwatch.basemap';

let current: BasemapId = DEFAULT_BASEMAP;

function apply(id: BasemapId): void {
  current = id;
  // Set synchronously, so a getComputedStyle read immediately afterwards sees the
  // new tokens — registerMarkerIcons does exactly that, and the satellite block in
  // tokens.css overrides --marker-halo. Same discipline as theme.ts::apply.
  document.documentElement.dataset['basemap'] = id;
}

/** Resolve and apply the stored basemap. Call once, before the first render. */
export function initBasemap(): BasemapId {
  let stored: string | null = null;
  try {
    stored = localStorage.getItem(STORAGE_KEY);
  } catch {
    // Private browsing or a blocked origin — fall back to the default.
  }
  apply(isBasemapId(stored) ? stored : DEFAULT_BASEMAP);
  return current;
}

export function currentBasemap(): BasemapId {
  return current;
}

export function setStoredBasemap(id: BasemapId): void {
  try {
    localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // Preference simply won't survive a reload; not worth failing the change.
  }
  apply(id);
}
