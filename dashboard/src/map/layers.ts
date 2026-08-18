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

import type { CircleLayerSpecification, ExpressionSpecification } from '@maplibre/maplibre-gl-style-spec';
import {
  haloColor,
  repairedColor,
  severityColorExpression,
  severityRadiusExpression,
} from '../severity.ts';

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
 * Individual clusters. Colour AND radius both encode severity, so the encoding
 * survives colour-vision deficiency and greyscale printing.
 *
 * `repaired` is painted off the severity ramp entirely — a repaired defect is a
 * different kind of thing, not a low severity. Repaired clusters are requested
 * deliberately (`include_repaired=true`) so marking one repaired doesn't make it
 * vanish, which would leave the operator with an open panel for something no
 * longer on the map and no route back to un-repair it.
 */
export function individualLayer(): CircleLayerSpecification {
  const radius = severityRadiusExpression();
  return {
    id: LAYER_INDIVIDUAL,
    type: 'circle',
    source: SOURCE_ID,
    'source-layer': SOURCE_LAYER,
    filter: BASE_INDIVIDUAL_FILTER,
    paint: {
      'circle-color': ['case', IS_REPAIRED, repairedColor(), severityColorExpression()],
      'circle-radius': [
        'interpolate',
        ['linear'],
        ['zoom'],
        13,
        ['*', 0.7, radius],
        18,
        radius,
      ],
      'circle-opacity': ['case', IS_REPAIRED, 0.45, 0.9],
      // A halo keeps markers legible over both pale roads and dark parkland;
      // without it they disappear against mid-tone areas of the basemap.
      'circle-stroke-width': 1.5,
      'circle-stroke-color': haloColor(),
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
export function aggregateLayer(): CircleLayerSpecification {
  return {
    id: LAYER_AGGREGATE,
    type: 'circle',
    source: SOURCE_ID,
    'source-layer': SOURCE_LAYER,
    filter: ['has', 'point_count'],
    paint: {
      'circle-color': severityColorExpression('max_severity'),
      'circle-radius': [
        'interpolate',
        ['linear'],
        ['coalesce', ['get', 'point_count'], 1],
        1,
        8,
        10,
        14,
        50,
        20,
        200,
        28,
      ],
      'circle-opacity': 0.8,
      'circle-stroke-width': 1.5,
      'circle-stroke-color': haloColor(),
    },
  };
}
