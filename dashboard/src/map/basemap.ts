/**
 * Basemap style.
 *
 * Raster OSM tiles: zero setup, and the only thing this module exports is a
 * style object — so switching to a self-hosted Protomaps PMTiles archive (which
 * §3.3 of the architecture doc specifies, and which a real pilot needs) is a
 * change to this one file.
 *
 * NOTE: OSM's tile usage policy makes these DEV ONLY. Do not point a
 * municipality at tile.openstreetmap.org.
 */

import type { StyleSpecification } from 'maplibre-gl';

export function basemapStyle(): StyleSpecification {
  return {
    version: 8,
    // No glyphs are declared, so no layer may use `text-field`. Counts are
    // encoded by circle radius instead — see layers.ts. Adding labels later
    // means self-hosting one font range under public/font/.
    sources: {
      osm: {
        type: 'raster',
        tiles: ['https://tile.openstreetmap.org/{z}/{x}/{y}.png'],
        tileSize: 256,
        maxzoom: 19,
        attribution: '© OpenStreetMap contributors',
      },
    },
    layers: [
      {
        id: 'osm',
        type: 'raster',
        source: 'osm',
        paint: {
          // Slightly muted so the severity markers carry the colour rather than
          // competing with the basemap.
          'raster-saturation': -0.35,
          'raster-brightness-min': 0.05,
        },
      },
    ],
  };
}
