import { describe, expect, it } from 'vitest';

import {
  aggregateCountLayer,
  aggregateLayer,
  type ClusterColors,
  FRAMES_MAX_ZOOM,
  FRAMES_MIN_ZOOM,
  framesLayer,
  individualLayer,
  OBSERVATIONS_MAX_ZOOM,
  OBSERVATIONS_MIN_ZOOM,
  observationsLayer,
} from './layers.ts';
import { EVENT_ROLES, FRAME_ROLES, iconName, iconRoles } from './marker-icons.ts';

// Sentinel colours, so a value leaking in from a --severity-* token is obvious.
const CLUSTER: ClusterColors = {
  tiers: ['#SEV1', '#SEV2', '#SEV3', '#SEV4'],
  unknown: '#UNRATED',
  repaired: '#REPAIRED',
  halo: '#HALO',
  selected: '#SELECTED',
};
const FRAME = { scored: '#SCORED', unscored: '#UNSCORED' };
const EVENT = { pothole: '#CLASS0', crack: '#CLASS4', other: '#NEUTRAL' };

const json = (v: unknown) => JSON.stringify(v);

describe('one shape per kind of thing', () => {
  it('draws clusters as circles and events and frames as symbols', () => {
    // Shape is the channel that survives greyscale, which is the test colour cannot
    // pass. Before this everything was a circle and an event was indistinguishable
    // from a cluster.
    expect(individualLayer(CLUSTER).type).toBe('circle');
    expect(aggregateLayer(CLUSTER).type).toBe('circle');
    expect(observationsLayer(EVENT).type).toBe('symbol');
    expect(framesLayer(FRAME).type).toBe('symbol');
  });

  it('names a triangle for an event and a square for a frame', () => {
    expect(json(observationsLayer(EVENT).layout)).toContain('rw-triangle-');
    expect(json(framesLayer(FRAME).layout)).toContain('rw-square-');
  });

  it('only ever names an icon that is actually registered', () => {
    // A `match` naming an unregistered image renders NOTHING, silently. So the
    // names the layers build and the names the registration loop creates have to be
    // the same set -- which is why iconName() is shared by both.
    const registered = new Set(iconRoles({
      eventPothole: 'a', eventCrack: 'b', eventOther: 'c',
      frameScored: 'd', frameUnscored: 'e',
    }).map((r) => r.name));

    for (const role of EVENT_ROLES) {
      expect(registered.has(iconName('triangle', role, false))).toBe(true);
      expect(registered.has(iconName('triangle', role, true))).toBe(true);
    }
    for (const role of FRAME_ROLES) {
      expect(registered.has(iconName('square', role, false))).toBe(true);
      expect(registered.has(iconName('square', role, true))).toBe(true);
    }
  });
});

describe('events no longer borrow the severity ramp', () => {
  it('takes no colour from a severity tier', () => {
    // THE COLLISION THIS FILE EXISTS FOR. observationsLayer used to read
    // --severity-4 / --severity-2 / --severity-unknown, so a pothole event was
    // bit-identical (#8c491a) to a Severe cluster and a crack event to a Moderate
    // one. tokens.css already forbade it: the categorical palette is "deliberately
    // separate from the severity ramp".
    const spec = json(observationsLayer(EVENT));
    for (const tier of CLUSTER.tiers) {
      expect(spec).not.toContain(tier);
    }
    expect(spec).not.toContain(CLUSTER.unknown);
  });

  it('and neither does the frames layer', () => {
    const spec = json(framesLayer(FRAME));
    for (const tier of CLUSTER.tiers) {
      expect(spec).not.toContain(tier);
    }
  });
});

describe('aggregate bins cannot be mistaken for clusters', () => {
  it('starts above the largest individual radius', () => {
    // A 1-point bin used to draw at 8, between Moderate (7) and High (9) -- and both
    // schemas are briefly co-rendered crossing z12->z13, so "40 defects here" and
    // "one defect here" looked the same.
    const radius = aggregateLayer(CLUSTER).paint?.['circle-radius'] as unknown[];
    const smallestBin = radius[4] as number;

    const individual = individualLayer(CLUSTER).paint?.['circle-radius'];
    const largestTier = Math.max(
      ...JSON.stringify(individual)
        .match(/\d+(\.\d+)?/g)!
        .map(Number),
    );
    expect(smallestBin).toBeGreaterThan(largestTier);
  });

  it('carries the count as a label, which a cluster never does', () => {
    const layout = json(aggregateCountLayer(CLUSTER).layout);
    expect(layout).toContain('point_count');
    expect(json(individualLayer(CLUSTER))).not.toContain('text-field');
  });

  it('labels only aggregate features', () => {
    expect(aggregateCountLayer(CLUSTER).filter).toEqual(['has', 'point_count']);
  });
});

describe('repaired is not the unrated grey', () => {
  it('paints repaired from its own token', () => {
    // Both are CIRCLES, so unlike events-vs-frames shape cannot separate them. The
    // old pair differed by a contrast ratio of 1.62 in light and 1.12 in dark.
    const color = json(individualLayer(CLUSTER).paint?.['circle-color']);
    expect(color).toContain('#REPAIRED');
    expect('#REPAIRED').not.toBe(CLUSTER.unknown);
  });
});

describe('state ordering, which is load-bearing', () => {
  it('tests unscored before paired on frames', () => {
    // An unscored frame is ALSO unpaired. Reversing these would render every
    // backlogged frame hollow and indistinguishable from a scored-but-unpaired one --
    // and the backlog is what this layer is best placed to surface.
    const layout = json(framesLayer(FRAME).layout);
    expect(layout.indexOf('detected')).toBeLessThan(layout.indexOf('paired'));
  });

  it('gives an unscored frame its own icon rather than the hollow one', () => {
    const layout = json(framesLayer(FRAME).layout);
    expect(layout).toContain('unscored');
  });
});

describe('zoom ranges', () => {
  it('starts each layer at the zoom its endpoint will serve', () => {
    // Both endpoints 400 below their floor and MapLibre never retries an errored
    // tile, so a layer minzoom below the server's leaves permanent holes.
    expect(framesLayer(FRAME).minzoom).toBe(FRAMES_MIN_ZOOM);
    expect(observationsLayer(EVENT).minzoom).toBe(OBSERVATIONS_MIN_ZOOM);
  });

  it('keeps both ranges non-empty, which otherwise presents as "no data"', () => {
    // An inverted or empty min/max range renders nothing and looks exactly like an
    // empty dataset. This project has already shipped one silent nothing-renders bug.
    expect(FRAMES_MAX_ZOOM).toBeGreaterThan(FRAMES_MIN_ZOOM);
    expect(OBSERVATIONS_MAX_ZOOM).toBeGreaterThan(OBSERVATIONS_MIN_ZOOM);
  });
});

describe('frame size still encodes the server score', () => {
  it('sizes on server_probability and not the device score', () => {
    const size = json(framesLayer(FRAME).layout?.['icon-size']);
    expect(size).toContain('server_probability');
    expect(size).not.toContain('device_probability');
  });

  it('interpolates monotonically over the whole range', () => {
    const size = framesLayer(FRAME).layout?.['icon-size'] as unknown[];
    const stops: [number, number][] = [];
    for (let i = 3; i < size.length; i += 2) {
      stops.push([size[i] as number, size[i + 1] as number]);
    }
    expect(stops[0]?.[0]).toBe(0);
    expect(stops[stops.length - 1]?.[0]).toBe(1);
    for (let i = 1; i < stops.length; i += 1) {
      expect(stops[i]![0]).toBeGreaterThan(stops[i - 1]![0]);
      expect(stops[i]![1]).toBeGreaterThan(stops[i - 1]![1]);
    }
  });
});

describe('dense point layers must not drop markers', () => {
  it('allows overlap on both symbol layers', () => {
    // "How many are here" is the question these layers answer. Letting MapLibre
    // collide-cull would silently hide readings.
    for (const layer of [observationsLayer(EVENT), framesLayer(FRAME)]) {
      expect(layer.layout?.['icon-allow-overlap']).toBe(true);
      expect(layer.layout?.['icon-ignore-placement']).toBe(true);
    }
  });
});
