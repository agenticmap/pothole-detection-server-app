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
 * A `Flavor` is ~75 colour fields; the named flavors supply sensible values for
 * all of them, so this overrides only what the design actually speaks to and
 * inherits the rest. Every value comes from tokens.css rather than a literal, so
 * the map and the chrome cannot drift apart — and because `cssVar` resolves
 * against the live `data-theme`, one function serves both themes.
 *
 * The shape of the edit, per the design handoff: land and water lifted toward
 * the canvas, road casing pulled to a neutral mid-grey, labels held at full text
 * contrast. Warmth stays in the palette — the point is a calm ground, not a grey
 * one.
 */
function organicFlavor(theme: Theme): Flavor {
  const base = namedFlavor(theme === 'dark' ? 'dark' : 'light');

  const land = cssVar('--map-land');
  const water = cssVar('--map-water');
  const green = cssVar('--map-green');
  const greenDeep = cssVar('--map-green-deep');
  const building = cssVar('--map-building');
  const institution = cssVar('--map-institution');
  const road = cssVar('--map-road');
  const casing = cssVar('--map-road-casing');
  const casingStrong = cssVar('--map-road-casing-strong');
  const text = cssVar('--color-text');
  const muted = cssVar('--color-text-muted');
  const halo = cssVar('--color-canvas');

  return {
    ...base,
    background: land,
    earth: land,
    water,
    // Green space keeps the sage voice the design system asks for as a genuine
    // second colour, rather than another tint of the terracotta primary.
    park_a: green,
    park_b: green,
    wood_a: greenDeep,
    wood_b: greenDeep,
    scrub_a: green,
    scrub_b: green,
    zoo: green,
    // Institutional land is a ground, not a category to read at a glance.
    hospital: institution,
    school: institution,
    industrial: institution,
    military: institution,
    aerodrome: institution,
    pedestrian: institution,
    buildings: building,
    // Roads read as light channels cut through the ground, with the casing
    // carrying the edge. This is the pairing that stops the network competing
    // with the markers.
    other: road,
    minor_service: road,
    minor_a: road,
    minor_b: road,
    link: road,
    major: road,
    highway: road,
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
    roads_label_minor: muted,
    roads_label_minor_halo: halo,
    roads_label_major: text,
    roads_label_major_halo: halo,
    subplace_label: muted,
    subplace_label_halo: halo,
    city_label: text,
    city_label_halo: halo,
    state_label: muted,
    state_label_halo: halo,
    ocean_label: cssVar('--color-accent-2-text'),
  };
}

export function basemapStyle(theme: Theme = 'light'): StyleSpecification {
  return {
    version: 8,
    glyphs: GLYPHS,
    sprite: `${SPRITE_BASE}/${theme === 'dark' ? 'dark' : 'light'}`,
    sources: {
      [BASEMAP_SOURCE_ID]: {
        type: 'vector',
        url: `pmtiles://${ARCHIVE_URL}`,
        attribution: '© OpenStreetMap contributors · Protomaps',
      },
    },
    layers: basemapLayers(theme),
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
