import { describe, expect, it } from 'vitest';

import { FRAMES_MAX_ZOOM, FRAMES_MIN_ZOOM, framesLayer } from './layers.ts';

const COLORS = { scored: '#SCORED', unscored: '#UNSCORED' };

describe('framesLayer', () => {
  it('starts at the zoom the endpoint will actually serve', () => {
    // The frames tile route 400s below tile_frames_min_zoom, and MapLibre never
    // retries a tile that errored. A layer minzoom below the server's floor would
    // therefore not just fail once -- it would leave permanent holes in the layer
    // for the rest of the session.
    expect(framesLayer(COLORS).minzoom).toBe(FRAMES_MIN_ZOOM);
  });

  it('keeps a non-empty zoom range, which otherwise presents as "no data"', () => {
    // An inverted or empty min/max range renders nothing at all, and looks exactly
    // like an empty dataset. This project has already shipped one silent
    // nothing-renders bug (the MapLibre worker 404).
    expect(FRAMES_MAX_ZOOM).toBeGreaterThan(FRAMES_MIN_ZOOM);
  });

  it('sizes markers by the SERVER score, not the device score', () => {
    const radius = framesLayer(COLORS).paint?.['circle-radius'];
    expect(JSON.stringify(radius)).toContain('server_probability');
    expect(JSON.stringify(radius)).not.toContain('device_probability');
  });

  it('interpolates radius monotonically over the whole probability range', () => {
    const radius = framesLayer(COLORS).paint?.['circle-radius'] as unknown[];
    // ['interpolate', ['linear'], expr, 0, r0, 0.5, r1, 1, r2]
    const stops: [number, number][] = [];
    for (let i = 3; i < radius.length; i += 2) {
      stops.push([radius[i] as number, radius[i + 1] as number]);
    }
    expect(stops[0]?.[0]).toBe(0);
    expect(stops[stops.length - 1]?.[0]).toBe(1);
    for (let i = 1; i < stops.length; i += 1) {
      expect(stops[i]![0]).toBeGreaterThan(stops[i - 1]![0]);
      expect(stops[i]![1]).toBeGreaterThan(stops[i - 1]![1]);
    }
  });

  it('checks UNSCORED before UNPAIRED, so an unscored frame reads grey not hollow', () => {
    // Order matters in a `case` expression: the first matching branch wins. An
    // unscored frame is also unpaired, so if the paired test came first every
    // unscored frame would render fully transparent -- invisible, which is the one
    // thing this layer exists to prevent ("the detection backlog is exactly the kind
    // of thing this layer is well placed to surface").
    const color = framesLayer(COLORS).paint?.['circle-color'] as unknown[];
    const asText = JSON.stringify(color);
    expect(asText.indexOf('detected')).toBeLessThan(asText.indexOf('paired'));
    // And the unscored branch resolves to the unscored colour, not to transparent.
    expect(color[2]).toBe(COLORS.unscored);
  });

  it('renders an unpaired frame hollow', () => {
    const color = framesLayer(COLORS).paint?.['circle-color'] as unknown[];
    expect(color[4]).toBe('rgba(0,0,0,0)');
  });

  it('keeps a visible stroke on a hollow marker', () => {
    // A hollow fill with no stroke is an invisible marker. The stroke is what makes
    // "did not contribute" readable rather than absent.
    const layer = framesLayer(COLORS);
    expect(layer.paint?.['circle-stroke-width']).toBeGreaterThan(0);
    expect(layer.paint?.['circle-stroke-opacity']).toBeGreaterThan(0);
  });

  it('takes its colours from the argument, so it needs no DOM', () => {
    const layer = framesLayer(COLORS);
    expect(JSON.stringify(layer.paint)).toContain('#SCORED');
    expect(JSON.stringify(layer.paint)).toContain('#UNSCORED');
  });
});
