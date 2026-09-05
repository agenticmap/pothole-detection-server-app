import { describe, expect, it } from 'vitest';
import { layers, namedFlavor } from '@protomaps/basemaps';

import {
  BASEMAPS,
  DEFAULT_BASEMAP,
  isBasemapId,
  satelliteFlavor,
  satelliteSource,
} from './basemaps';

// Deliberately NOT importing ./basemap.ts here: it pulls in maplibre-gl, which
// touches `window` at import time, and this suite runs in the node environment.
// That constraint is why the registry lives in its own module.

describe('the basemap registry', () => {
  it('names only flavors the library actually has', () => {
    // The highest-value assertion in the file. namedFlavor is a switch over five
    // names and THROWS `Flavor not found` on anything else — including 'grey' and
    // 'gray', the two spellings a reader reaches for first. A wrong name here is
    // not a rendering glitch; it is an exception at the moment an operator picks
    // that option, with no way back except a reload.
    for (const option of BASEMAPS) {
      if (!option.flavor) continue;
      expect(() => namedFlavor(option.flavor!), option.id).not.toThrow();
    }
  });

  it('spells greyscale the library way, not the house way', () => {
    const greyscale = BASEMAPS.find((option) => option.id === 'grayscale');
    expect(greyscale?.flavor).toBe('grayscale');
    // The label may read however the console prefers; the id may not.
    expect(greyscale?.label).toBe('Greyscale');
    expect(() => namedFlavor('grey')).toThrow();
  });

  it('defaults to organic, the only theme-derived option', () => {
    expect(DEFAULT_BASEMAP).toBe('organic');
    // Pins the applyTheme contract: everything else keeps its own palette across a
    // theme flip, which is why the flip still has to re-style for the markers.
    expect(BASEMAPS.filter((option) => option.themed).map((option) => option.id)).toEqual([
      'organic',
    ]);
  });

  it('rejects near-miss ids rather than passing them to namedFlavor', () => {
    // This is what makes a hand-edited localStorage value fall back to organic
    // instead of throwing on the next load.
    for (const bad of ['grey', 'gray', '', 'satellite ', 'Organic', null, undefined, 7]) {
      expect(isBasemapId(bad), String(bad)).toBe(false);
    }
    for (const option of BASEMAPS) {
      expect(isBasemapId(option.id), option.id).toBe(true);
    }
  });
});

describe('satellite street labels', () => {
  it('needs BOTH labelsOnly and lang, or it silently renders nothing', () => {
    // The upstream trap: layers() builds the label layers in its `lang` branch, so
    // labelsOnly on its own returns an empty array — imagery with no street names
    // and no error. If a @protomaps/basemaps bump renames either option, this is
    // the test that says so.
    expect(layers('src', satelliteFlavor(), { labelsOnly: true })).toEqual([]);
    const labels = layers('src', satelliteFlavor(), { labelsOnly: true, lang: 'en' });
    expect(labels.length).toBeGreaterThan(0);
    for (const layer of labels) {
      expect(layer.type).toBe('symbol');
      expect('source' in layer && layer.source).toBe('src');
    }
  });

  it('does not reuse a palette tuned for a pale ground', () => {
    const flavor = satelliteFlavor();
    // Guards the legibility decision against a well-meaning refactor that points
    // this back at organicFlavor: those halos are --map-land, a pale cream, which
    // vanishes over a photograph.
    expect(flavor.roads_label_major).not.toBe(namedFlavor('light').roads_label_major);
    expect(flavor.roads_label_major).not.toBe(flavor.roads_label_major_halo);
    expect(flavor.city_label).not.toBe(flavor.city_label_halo);
  });
});

describe('the Esri imagery source', () => {
  it('is a raster source that credits Esri', () => {
    const source = satelliteSource();
    expect(source.type).toBe('raster');
    expect(source.tileSize).toBe(256);
    // Attribution is a licence requirement, not decoration: MapLibre's
    // AttributionControl reads it straight off the source.
    expect(source.attribution).toContain('Esri');
  });

  it('caps at zoom 19, because out of coverage Esri answers 200, not 404', () => {
    // A "no data" placeholder arrives with a 200, so MapLibre's error path never
    // fires and the operator would just see grey squares they cannot distinguish
    // from a real parking lot. Capped, MapLibre overzooms a genuine tile.
    expect(satelliteSource().maxzoom).toBe(19);
  });

  it('orders the ArcGIS path z/y/x, not z/x/y', () => {
    // ArcGIS REST is /{level}/{row}/{col}. MapLibre substitutes by name, so only
    // the order in the path distinguishes correct imagery from transposed imagery
    // — and transposed imagery reads as a network fault rather than a typo.
    const url = satelliteSource().tiles?.[0] ?? '';
    expect(url).toContain('/{z}/{y}/{x}');
    expect(url).not.toContain('/{z}/{x}/{y}');
  });
});
