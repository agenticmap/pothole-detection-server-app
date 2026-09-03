/**
 * Style layers for the cluster source.
 *
 * The source's feature schema changes with zoom: at or below
 * `tile_aggregate_max_zoom` (12) the server returns grid-aggregated bins carrying
 * `point_count` / `max_severity`; above it, individual clusters carrying
 * `cluster_id`, `severity`, `repaired` and friends. Two layers separated by a
 * filter on `['has', 'point_count']` is the correct way to render that — MapLibre
 * filters are per-feature, so a mixed source is fine.
 *
 * Both schemas can be on screen at once for a moment: MapLibre renders a scaled
 * parent tile until the child loads, so crossing z12→z13 briefly shows aggregate
 * bubbles at z13. Anything reading feature properties must tolerate that — which
 * is why the click handler binds to the individual layer id and still guards on
 * `cluster_id` being present.
 *
 * Every numeric `get` is wrapped in `coalesce`. ST_AsMVT omits NULL attributes
 * from a tile entirely — the key is simply absent from the feature — and
 * `asset_cluster.severity` is nullable, so a bare `['get','severity']` feeds null
 * into a step expression and MapLibre raises a render-time expression error.
 */

import type {
  CircleLayerSpecification,
  ExpressionSpecification,
  SymbolLayerSpecification,
} from '@maplibre/maplibre-gl-style-spec';
import {
  haloColor,
  repairedColor,
  selectedRingColor,
  severityColorExpression,
  severityColors,
  severityRadiusExpression,
  unknownColor,
} from '../severity.ts';
import { cssVar } from '../tokens.ts';

export const SOURCE_ID = 'clusters';
export const SOURCE_LAYER = 'clusters';
export const LAYER_INDIVIDUAL = 'clusters-individual';
export const LAYER_AGGREGATE = 'clusters-aggregate';

/**
 * What makes a feature an individual cluster rather than an aggregate bin.
 *
 * Exported because the dock's filter has to AND onto it: replacing the layer
 * filter outright would let aggregate bins render through the individual layer
 * at low zoom, painted off a `severity` they do not have.
 */
export const BASE_INDIVIDUAL_FILTER: ExpressionSpecification = ['!', ['has', 'point_count']];

/** True when the feature is repaired, by tile attribute or optimistic feature state. */
const IS_REPAIRED: ExpressionSpecification = [
  'any',
  ['boolean', ['feature-state', 'repaired'], false],
  ['coalesce', ['get', 'repaired'], false],
];

/**
 * True when this is the open cluster. Set from map.ts via setFeatureState, the
 * same channel markRepairedOptimistically uses.
 */
const IS_SELECTED: ExpressionSpecification = ['boolean', ['feature-state', 'selected'], false];

/**
 * Individual clusters. Colour AND radius both encode severity, so the encoding
 * survives colour-vision deficiency and greyscale printing.
 *
 * `repaired` is painted off the severity ramp entirely — a repaired defect is a
 * different kind of thing, not a low severity. Repaired clusters are requested
 * deliberately (`include_repaired=true`) so marking one repaired doesn't make it
 * vanish, which would leave the operator with an open panel for something no
 * longer on the map and no route back to un-repair it.
 */
export interface ClusterColors {
  tiers: readonly string[];
  unknown: string;
  repaired: string;
  halo: string;
  selected: string;
}

function defaultClusterColors(): ClusterColors {
  return {
    tiers: severityColors(),
    unknown: unknownColor(),
    repaired: repairedColor(),
    halo: haloColor(),
    selected: selectedRingColor(),
  };
}

export function individualLayer(
  colors: ClusterColors = defaultClusterColors(),
): CircleLayerSpecification {
  const radius = severityRadiusExpression();
  const severity = severityColorExpression('severity', colors);
  return {
    id: LAYER_INDIVIDUAL,
    type: 'circle',
    source: SOURCE_ID,
    'source-layer': SOURCE_LAYER,
    filter: BASE_INDIVIDUAL_FILTER,
    paint: {
      'circle-color': ['case', IS_REPAIRED, colors.repaired, severity],
      // Radius is FIXED per tier, not interpolated by zoom. An earlier version
      // scaled from 0.7x at z13 up to 1x at z18, which made every marker tiny at
      // the default z14 and flattened the four tiers into near-identical dots —
      // the tier radii are the redundant channel that carries severity when colour
      // cannot, so shrinking them defeats the encoding.
      'circle-radius': ['case', IS_SELECTED, ['*', 1.5, radius], radius],
      'circle-opacity': ['case', IS_REPAIRED, 0.55, 0.95],
      // A halo keeps markers legible over both pale roads and dark parkland;
      // without it they disappear against mid-tone areas of the basemap.
      'circle-stroke-width': ['case', IS_SELECTED, 2.5, 1.5],
      'circle-stroke-color': ['case', IS_SELECTED, colors.selected, colors.halo],
      'circle-stroke-opacity': ['case', IS_REPAIRED, 0.5, 1],
    },
  };
}

/**
 * Low-zoom aggregate bins.
 *
 * Count is encoded by radius rather than a numeric label. That was originally a
 * hard constraint — the raster style declared no `glyphs` URL, and a `text-field`
 * without one silently renders nothing — but the Protomaps basemap declares
 * glyphs, so a numeric label is now possible. Radius is kept because it is the
 * channel that survives colour-vision deficiency and greyscale printing; adding
 * a label on top would be an improvement, not a fix.
 */
export function aggregateLayer(
  colors: ClusterColors = defaultClusterColors(),
): CircleLayerSpecification {
  return {
    id: LAYER_AGGREGATE,
    type: 'circle',
    source: SOURCE_ID,
    'source-layer': SOURCE_LAYER,
    filter: ['has', 'point_count'],
    paint: {
      'circle-color': severityColorExpression('max_severity', colors),
      // Starts at 13, ABOVE the largest individual tier (11), so the two ranges
      // cannot overlap. They used to: a 1-point bin drew at 8, between Moderate (7)
      // and High (9), and both schemas are briefly on screen together crossing
      // z12->z13 -- so "40 defects here" and "one defect here" rendered identically.
      'circle-radius': [
        'interpolate',
        ['linear'],
        ['coalesce', ['get', 'point_count'], 1],
        1,
        13,
        10,
        18,
        50,
        23,
        200,
        30,
      ],
      'circle-opacity': 0.8,
      'circle-stroke-width': 1.5,
      'circle-stroke-color': colors.halo,
      'circle-stroke-opacity': 1,
    },
  };
}

export const LAYER_AGGREGATE_COUNT = 'clusters-aggregate-count';

/**
 * The count, written on the aggregate bin.
 *
 * A separate symbol layer rather than a label on the circle layer, because MapLibre
 * keeps circle and symbol paint in different layer types. It is what finally makes a
 * bin unmistakable for a cluster: radius alone said "bigger", which a High-severity
 * cluster also says, and the two schemas are briefly co-rendered crossing z12->z13.
 *
 * `glyphs` is declared by the Protomaps basemap style, so `text-field` renders. A
 * digit is safely inside any font's coverage, which is why the count is a label and
 * the SHAPES are bitmaps -- see map/marker-icons.ts.
 */
export function aggregateCountLayer(
  colors: ClusterColors = defaultClusterColors(),
): SymbolLayerSpecification {
  return {
    id: LAYER_AGGREGATE_COUNT,
    type: 'symbol',
    source: SOURCE_ID,
    'source-layer': SOURCE_LAYER,
    filter: ['has', 'point_count'],
    layout: {
      'text-field': ['to-string', ['coalesce', ['get', 'point_count'], '']],
      'text-size': 11,
      'text-allow-overlap': true,
      'text-ignore-placement': true,
    },
    paint: {
      // The halo is the surface colour, so the digits read on every ramp step --
      // severity-1 is nearly the halo colour and severity-4 is nearly black.
      'text-color': colors.selected,
      'text-halo-color': colors.halo,
      'text-halo-width': 1.4,
    },
  };
}

// ── Raw observations ────────────────────────────────────────────────────────
//
// A second source, deliberately separate from the clusters one: it is a
// different endpoint with a different schema and a hard server-side zoom floor.
//
// Why it exists at all. A cluster needs `cluster_min_points` (3) admitted
// detections within `cluster_eps_m` (25 m). On the 2026-08 drives, 166
// observations are admitted and only 110 land in a cluster — so a third of the
// potholes the sensor actually reported are, without this layer, visible at no
// zoom on any surface. "Show the clusters" and "show what was reported" are not
// the same request.
//
// The tile endpoint applies NO class or outlier filter (app/services/
// tile_service.py) — it emits sensor_class / sensor_is_outlier / sensor_p_pothole
// and lets the client decide. That is what makes the outlier gate inspectable
// from the map, which matters because that gate is the single biggest lever on
// what reaches the crowd pipeline.

export const SOURCE_OBSERVATIONS = 'observations';
export const SOURCE_LAYER_OBSERVATIONS = 'observations';
export const LAYER_OBSERVATIONS = 'observations-points';

/**
 * Server-side minimum zoom for the observations endpoint.
 *
 * Mirrors `tile_observations_min_zoom` in app/config.py. This MUST be set as the
 * source's `minzoom`: below it the endpoint returns HTTP 400, and an errored
 * MapLibre tile never retries — so a single request at z14 leaves a permanently
 * dead tile that only a source rebuild clears.
 */
export const OBSERVATIONS_MIN_ZOOM = 15;

/**
 * Source maxzoom for observations. MUST be > OBSERVATIONS_MIN_ZOOM: a vector
 * source with maxzoom below its minzoom has an empty range and silently never
 * loads a tile. It cannot reuse the clusters' SOURCE_MAX_ZOOM (14) for exactly
 * that reason.
 *
 * 16 means real tiles at z15-16 and client-side overzoom above, which keeps the
 * PostGIS query count down at the street zooms where this layer is used.
 */
export const OBSERVATIONS_MAX_ZOOM = 16;

/** True when the sensor model's outlier gate rejected this observation. */
const IS_OUTLIER: ExpressionSpecification = ['coalesce', ['get', 'sensor_is_outlier'], false];

/**
 * Raw observations, styled by class and outlier flag.
 *
 * Subordinate to clusters by construction — smaller radius, lower opacity, and
 * added beneath them — because a cluster is corroborated evidence and a lone
 * observation is not. They must not compete for attention.
 *
 * Outliers are drawn HOLLOW rather than hidden. They are the rows the member
 * gate silently drops, and 31.7% of pothole-classed observations still carry the
 * flag even after the class-neutral feature set; an operator seeing a ring of
 * hollow dots around a solid one is seeing a real property of the pipeline, not
 * noise. Hiding them would make the gate unfalsifiable from the UI.
 */
export interface ObservationColors {
  pothole: string;
  crack: string;
  other: string;
}

/**
 * Sensor events, as TRIANGLES coloured by class.
 *
 * **The classes no longer borrow the severity ramp.** They used to read
 * `--severity-4` / `--severity-2` / `--severity-unknown`, which made a pothole event
 * bit-identical to a Severe cluster and a crack event to a Moderate one — on a map
 * where 306 pothole events sit among 5,686 dots. `tokens.css` already wrote the rule
 * that forbids this: the `--review-class-*` palette is "a CATEGORICAL palette,
 * deliberately separate from the severity ramp… a class is a third kind of thing and
 * borrowing either would misread." The review surface obeyed it; the map did not.
 *
 * Colours are injectable for the same reason `framesLayer`'s are: `cssVar` reads the
 * DOM, and the suite is node-environment with no jsdom, so an un-injectable layer
 * cannot be tested at all.
 */
export function observationsLayer(
  colors: ObservationColors = {
    pothole: cssVar('--review-class-0'),
    crack: cssVar('--review-class-4'),
    other: cssVar('--marker-neutral'),
  },
): SymbolLayerSpecification {
  void colors; // the colours are baked into the registered bitmaps, not read here
  return {
    id: LAYER_OBSERVATIONS,
    type: 'symbol',
    source: SOURCE_OBSERVATIONS,
    'source-layer': SOURCE_LAYER_OBSERVATIONS,
    minzoom: OBSERVATIONS_MIN_ZOOM,
    layout: {
      // Hollow for outliers: the same "did not contribute" grammar the frames layer
      // uses, now carried by a hollow bitmap rather than a transparent fill.
      'icon-image': [
        'concat',
        'rw-triangle-',
        [
          'match',
          ['coalesce', ['get', 'sensor_class'], 'not'],
          'pothole', 'pothole',
          'crack', 'crack',
          'other',
        ],
        ['case', IS_OUTLIER, '-hollow', ''],
      ],
      'icon-size': 0.62,
      // These are dense point data; letting MapLibre drop overlapping markers would
      // silently hide readings, and "how many are here" is the question the layer
      // exists to answer.
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
  };
}

// ── Camera frames ───────────────────────────────────────────────────────────
//
// The third source, and the visual counterpart to the observations layer. That
// one answers "where did a wheel hit something?"; this answers "where did the
// camera think it saw a defect?", which is a genuinely different set -- 98.6% of
// pothole-classed observations have no coincident frame at all, and the frames
// outnumber the observations.
//
// The tile carries `paired` and `fused_confidence` because the interesting
// question about a camera detection is not its score but whether it reached
// fusion. A frame that scored 0.9 and paired with nothing contributed nothing to
// any cluster, and the score alone cannot say so.

export const SOURCE_FRAMES = 'frames';
export const SOURCE_LAYER_FRAMES = 'frames';
export const LAYER_FRAMES = 'frames-points';

/** Mirrors `tile_frames_min_zoom` in app/config.py; the endpoint 400s below it. */
export const FRAMES_MIN_ZOOM = 15;
/** Must exceed FRAMES_MIN_ZOOM — see OBSERVATIONS_MAX_ZOOM for why. */
export const FRAMES_MAX_ZOOM = 16;

/** The detector never ran on this frame: `detected_at IS NULL`. */
const IS_UNSCORED: ExpressionSpecification = ['!', ['coalesce', ['get', 'detected'], false]];

/** Reached fusion — i.e. actually contributed to a cluster's evidence. */
const IS_PAIRED: ExpressionSpecification = ['coalesce', ['get', 'paired'], false];

/**
 * Camera frames, styled by detector confidence and fusion outcome.
 *
 * **Hollow means "did not contribute"** — the same grammar the observations
 * layer uses for outlier-rejected readings. There it is the outlier gate; here it
 * is an unpaired frame. An operator who learns the rule once reads both layers.
 *
 * Radius encodes `server_probability` so a confident detection is findable among
 * thousands of near-zero ones; mean score on the collected data is 0.151, with
 * only 352 of 5,615 above 0.5, so without this the layer is a uniform smear.
 *
 * Unscored frames are grey and small rather than hidden: the detection backlog is
 * exactly the kind of thing this layer is well placed to surface.
 */
export interface FrameColors {
  scored: string;
  unscored: string;
}

/**
 * Camera frames, as SQUARES sized by detector confidence.
 *
 * A square because a frame is an IMAGE — a different kind of thing from a cluster
 * (circle) and from a sensor event (triangle). Before this they were circles whose
 * radius differed from an event's by half a pixel, sharing the same opacity, stroke
 * width and stroke opacity, so hue was the only separator between two layers that can
 * both be on screen at once with ~10,000 markers between them.
 *
 * **Hollow means "did not contribute"** — the same grammar the events layer uses for
 * an outlier-rejected reading. An operator who learns the rule once reads both layers.
 *
 * Size encodes `server_probability` so a confident detection is findable among
 * thousands of near-zero ones: the mean score on this corpus is 0.177 and only a
 * fraction clear 0.5, so without it the layer is a uniform smear.
 *
 * Unscored frames are neutral and small rather than hidden: the detection backlog is
 * exactly the kind of thing this layer is well placed to surface.
 */
export function framesLayer(
  // Injectable so the expression structure can be asserted without a DOM: cssVar()
  // reads getComputedStyle(document.documentElement), and the suite is deliberately
  // node-environment with no jsdom. The default is exactly the previous behaviour, so
  // no call site changes. MapLibre has no notion of var(), which is the one place in
  // this codebase where resolving a colour into JS is correct.
  colors: FrameColors = {
    scored: cssVar('--color-accent-2'),
    unscored: cssVar('--marker-neutral'),
  },
): SymbolLayerSpecification {
  void colors; // baked into the registered bitmaps, not read as a paint property
  return {
    id: LAYER_FRAMES,
    type: 'symbol',
    source: SOURCE_FRAMES,
    'source-layer': SOURCE_LAYER_FRAMES,
    minzoom: FRAMES_MIN_ZOOM,
    layout: {
      // IS_UNSCORED is tested BEFORE the paired check, and the order is load-bearing:
      // an unscored frame is also unpaired, so testing paired first would render every
      // backlogged frame hollow and indistinguishable from a scored-but-unpaired one.
      'icon-image': [
        'concat',
        'rw-square-',
        ['case', IS_UNSCORED, 'unscored', 'scored'],
        ['case', IS_UNSCORED, '', ['!', IS_PAIRED], '-hollow', ''],
      ],
      'icon-size': [
        'interpolate',
        ['linear'],
        ['coalesce', ['get', 'server_probability'], 0],
        0,
        0.4,
        0.5,
        0.62,
        1,
        0.85,
      ],
      'icon-allow-overlap': true,
      'icon-ignore-placement': true,
    },
  };
}
