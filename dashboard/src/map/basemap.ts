/**
 * Basemap style — self-hosted Protomaps vector tiles.
 *
 * This replaces the raster OSM tiles Phase 2.5 shipped. Raster was always a
 * placeholder: OSM's tile usage policy forbids pointing a municipality at
 * tile.openstreetmap.org, and §3.3 of the architecture doc specifies Protomaps
 * PMTiles. The visual argument is the stronger one, though — a raster tile
 * exposes only `raster-*` paint, so the whole picture can only be desaturated or
 * dimmed as one. The design calls for the *map* to recede and the severity
 * markers to be the only saturated thing on screen, which means restyling land,
 * water, road casing and labels independently. That is a vector-only operation.
 *
 * The archive is one file served over HTTP range requests — no tile server. See
 * dashboard/README.md for how to regenerate it with the `pmtiles` CLI.
 */

import { addProtocol } from 'maplibre-gl';
import { Protocol } from 'pmtiles';
import { layers, namedFlavor, type Flavor } from '@protomaps/basemaps';
import type { LayerSpecification, StyleSpecification } from '@maplibre/maplibre-gl-style-spec';
import { cssVar } from '../tokens.ts';
import type { Theme } from '../theme.ts';
import {
  basemapOption,
  DEFAULT_BASEMAP,
  SATELLITE_BACKDROP,
  SATELLITE_SOURCE_ID,
  satelliteFlavor,
  satelliteSource,
  type BasemapId,
} from './basemaps.ts';

export const BASEMAP_SOURCE_ID = 'protomaps';

/**
 * Where the archive lives. Root-relative so it rides the same origin as the API
 * (app/main.py mounts it at /basemap), which keeps it out of the Vite bundle and
 * lets a deployment swap in a bigger region without a rebuild.
 */
const ARCHIVE_URL = import.meta.env['VITE_BASEMAP_URL'] ?? '/basemap/toronto.pmtiles';

/**
 * Glyphs and sprites. **Not optional** — a style whose layers use `text-field`
 * without a `glyphs` URL renders no labels at all, silently.
 *
 * These are Protomaps' hosted assets, which is fine for development. A pilot
 * should self-host them under public/basemap-assets/: a municipal tool that goes
 * blank-labelled when a GitHub Pages origin has a bad day is not acceptable, and
 * it is a third-party runtime dependency in the request path.
 */
const GLYPHS = 'https://protomaps.github.io/basemaps-assets/fonts/{fontstack}/{range}.pbf';
const SPRITE_BASE = 'https://protomaps.github.io/basemaps-assets/sprites/v4';

/**
 * Register the pmtiles:// protocol.
 *
 * Module scope on purpose: `addProtocol` is global to MapLibre, and registering
 * per-map would re-register on every sign-in. The worker delegates protocols it
 * does not recognise back to the main thread, so a single main-thread
 * registration is enough for vector tiles.
 */
const protocol = new Protocol();
addProtocol('pmtiles', protocol.tile);

/**
 * The Organic flavor.
 *
 * `namedFlavor('light')` is the base, and it is kept for a specific reason: it
 * already encodes OSM's ROAD HIERARCHY — white major roads over #ebebeb minor
 * ones, with casings a step darker. That hierarchy is what makes a street network
 * legible, and the first version of this file destroyed it by painting every road
 * class the same cream. Only the palette is re-pointed here; the structure is
 * Protomaps'.
 *
 * What the base gets wrong for this design, and is overridden below: its water is
 * a vivid cyan (#80deea) and its land is a cool grey (#e2dfda). Both are pulled
 * into the warm, drained register of the source mockup.
 *
 * Every value comes from the --map-* block in styles.css rather than a literal,
 * so light and dark are declared in one place and `cssVar` resolves against the
 * live `data-theme`.
 */
function organicFlavor(theme: Theme): Flavor {
  const base = namedFlavor(theme === 'dark' ? 'dark' : 'light');

  const land = cssVar('--map-land');
  const road = cssVar('--map-road');
  const roadMajor = cssVar('--map-road-major');
  const roadHighway = cssVar('--map-road-highway');
  const casing = cssVar('--map-road-casing');
  const casingStrong = cssVar('--map-road-casing-strong');
  const green = cssVar('--map-green');
  const greenDeep = cssVar('--map-green-deep');
  const institution = cssVar('--map-institution');

  return {
    ...base,
    background: cssVar('--map-backdrop'),
    earth: land,
    water: cssVar('--map-water'),
    buildings: cssVar('--map-building'),

    // Green space keeps the sage voice the design system asks for as a genuine
    // second colour. The _a / _b pairs are the low- and high-zoom variants, so
    // _b is a step deeper to keep parks from flattening as you zoom in.
    park_a: green,
    park_b: greenDeep,
    wood_a: green,
    wood_b: greenDeep,
    scrub_a: green,
    scrub_b: greenDeep,
    zoo: green,

    // Institutional land is a ground, not a category to read at a glance — one
    // step off the land rather than four different tints competing with markers.
    hospital: institution,
    school: institution,
    industrial: institution,
    military: institution,
    aerodrome: institution,
    pedestrian: institution,

    // Roads: lighter than the land, with majors carrying a warm tint so the
    // arterial network still reads at a glance — OSM's yellow, drained.
    other: road,
    minor_service: road,
    minor_a: road,
    minor_b: road,
    link: roadMajor,
    major: roadMajor,
    highway: roadHighway,
    minor_service_casing: casing,
    minor_casing: casing,
    link_casing: casing,
    major_casing_early: casing,
    major_casing_late: casing,
    highway_casing_early: casingStrong,
    highway_casing_late: casingStrong,
    railway: casingStrong,
    boundaries: casingStrong,

    // Labels are the one thing NOT calmed: an operator reads street names off
    // this map to dispatch a crew.
    roads_label_minor: cssVar('--color-text-muted'),
    roads_label_minor_halo: land,
    roads_label_major: cssVar('--color-text'),
    roads_label_major_halo: land,
    subplace_label: cssVar('--color-text-muted'),
    subplace_label_halo: land,
    city_label: cssVar('--color-text'),
    city_label_halo: land,
    state_label: cssVar('--color-text-muted'),
    state_label_halo: land,
    ocean_label: cssVar('--color-accent-2-text'),
  };
}

/**
 * The style for a chosen basemap.
 *
 * Three shapes behind one entry point: the Organic flavor (theme-derived), one of
 * Protomaps' five named flavors (fixed, regardless of the UI theme), and imagery.
 * `theme` is only consulted by the first — see basemaps.ts::BasemapOption.themed.
 */
export function basemapStyle(
  id: BasemapId = DEFAULT_BASEMAP,
  theme: Theme = 'light',
): StyleSpecification {
  if (id === 'satellite') return satelliteStyle();
  const flavor = basemapOption(id).flavor;
  return flavor ? namedStyle(flavor) : organicStyle(theme);
}

/** The archive, declared identically wherever a style needs the vector source. */
function vectorSource(): StyleSpecification['sources'][string] {
  return {
    type: 'vector',
    url: `pmtiles://${ARCHIVE_URL}`,
    attribution: '© OpenStreetMap contributors · Protomaps',
  };
}

function organicStyle(theme: Theme): StyleSpecification {
  return {
    version: 8,
    glyphs: GLYPHS,
    sprite: `${SPRITE_BASE}/${theme === 'dark' ? 'dark' : 'light'}`,
    sources: { [BASEMAP_SOURCE_ID]: vectorSource() },
    layers: basemapLayers(theme),
  };
}

/**
 * One of Protomaps' own flavors, unmodified.
 *
 * The sprite follows the FLAVOR, not the UI theme: picking the dark basemap under
 * a light console is a legitimate choice, and pairing it with the light sprite
 * sheet would put dark icons on a dark ground. All five names have a published v4
 * sprite.
 */
function namedStyle(flavor: string): StyleSpecification {
  return {
    version: 8,
    glyphs: GLYPHS,
    sprite: `${SPRITE_BASE}/${flavor}`,
    sources: { [BASEMAP_SOURCE_ID]: vectorSource() },
    // The POI filter applies here for both of the reasons it applies to Organic:
    // pins compete with the severity markers, and @protomaps/basemaps 5.7 asks for
    // icons the published v4 sprite sheet does not carry.
    layers: layers(BASEMAP_SOURCE_ID, namedFlavor(flavor), { lang: 'en' }).filter(
      (layer) => layer.id !== 'pois',
    ),
  };
}

/**
 * Aerial imagery, with the street labels kept on top.
 *
 * The labels are not a nicety: this file already states twice that an operator
 * reads street names off this map to dispatch a crew, and imagery without them is
 * imagery you cannot dispatch from. So the PMTiles source is still loaded here —
 * for the names alone — over the raster.
 *
 * The imagery also outruns the archive, which is the practical reason to offer it:
 * the PMTiles cut stops at z14 and MapLibre merely overzooms above that, while
 * Esri carries real detail to 19 — the range where someone decides whether a
 * cluster is a defect or a manhole.
 */
function satelliteStyle(): StyleSpecification {
  return {
    version: 8,
    // Required even though the raster itself needs no glyphs: the label layers
    // below use `text-field`, and a style with text-field and no `glyphs` URL
    // renders no labels at all, SILENTLY.
    glyphs: GLYPHS,
    // Kept so a future icon-bearing label layer does not warn once per feature.
    sprite: `${SPRITE_BASE}/dark`,
    sources: {
      [SATELLITE_SOURCE_ID]: satelliteSource(),
      [BASEMAP_SOURCE_ID]: vectorSource(),
    },
    layers: [
      // Stops a flash of the page ground before the first imagery tiles land.
      {
        id: 'background',
        type: 'background',
        paint: { 'background-color': SATELLITE_BACKDROP },
      },
      { id: 'satellite', type: 'raster', source: SATELLITE_SOURCE_ID },
      // BOTH options are required. `labelsOnly` alone returns an EMPTY array —
      // the label layers are produced by the `lang` branch, so omitting it yields
      // imagery with no street names and no error. basemaps.spec.ts pins this.
      ...layers(BASEMAP_SOURCE_ID, satelliteFlavor(), { labelsOnly: true, lang: 'en' }),
    ],
  };
}

/**
 * The flavor's layers, minus the POI pins.
 *
 * Two reasons, and the design one is the real one: a screen full of restaurant
 * and school pins competes directly with the severity markers, and the whole
 * point of the calmed basemap is that the markers are the only saturated thing
 * on it. Dropping them also silences a genuine upstream gap — @protomaps/basemaps
 * 5.7 references icons (e.g. "townhall") that the newest published sprite sheet,
 * v4, does not contain, so MapLibre warns once per missing icon.
 *
 * Street and place labels stay: an operator reads those to dispatch a crew.
 */
function basemapLayers(theme: Theme): LayerSpecification[] {
  return layers(BASEMAP_SOURCE_ID, organicFlavor(theme), { lang: 'en' }).filter(
    (layer) => layer.id !== 'pois',
  );
}
