/**
 * The severity encoding — one source of truth.
 *
 * The map layer, the legend and the detail panel all need this. Three copies of
 * a colour ramp is exactly how a legend silently stops matching its markers, so
 * they all import from here.
 *
 * Severity is ordinal, so it is encoded as a sequential ramp (see tokens.css for
 * why it is not red/amber/green). Colour is never the only channel: radius
 * scales too, and the panel prints the number.
 */

import type { ExpressionSpecification } from '@maplibre/maplibre-gl-style-spec';
import { cssVar } from './tokens.ts';

export interface SeverityTier {
  /** Inclusive lower bound of the tier. */
  min: number;
  label: string;
  /** CSS custom property holding the colour. */
  varName: string;
  /** Map marker radius in px at the reference zoom. */
  radius: number;
}

/**
 * Tier boundaries, on the unit scale the pipeline actually emits.
 *
 * `asset_cluster.severity` is the median of its members' `sensor_severity`
 * (app/fusion/service.py), and that is an IRI-style proxy **hard-clamped to
 * [0, 1]** by app/sensor_model/features.py:
 *
 *     severity = clamp(severity_scale * magnitude / max(speed, severity_speed_ref), 0, 1)
 *
 * with `severity_speed_ref = 5.0` (app/config.py).
 * An earlier version of this file described severity as "unbounded in principle,
 * in practice single digits" and put the floors at 0 / 1.5 / 3 / 5 — above the
 * ceiling. Every real cluster therefore painted in the first tier at the smallest
 * radius, and three of the four ramp colours were unreachable.
 *
 * The floors below are quartiles of the *possible* range. They now work, but only
 * because the other half of the problem was fixed on the server: `severity_scale`
 * was 2.0, which saturates at ratio >= 0.5 — the MINIMUM of the observed pothole
 * distribution — so every cluster scored exactly 1.0 and the ramp collapsed into
 * the top tier instead of the bottom one. Measured on the 2026-08 drives it is
 * now 0.25, fitted to p95 of magnitude/max(speed, 5) over cluster-admitted
 * potholes, which spreads 25 clusters across all four tiers as 2 / 12 / 9 / 2.
 *
 * The lesson to carry: these floors and `severity_scale` are one calibration in
 * two files. Changing either alone re-breaks the ramp.
 */
export const SEVERITY_TIERS: readonly SeverityTier[] = [
  { min: 0, label: 'Low', varName: '--severity-1', radius: 5 },
  { min: 0.25, label: 'Moderate', varName: '--severity-2', radius: 7 },
  { min: 0.5, label: 'High', varName: '--severity-3', radius: 9 },
  { min: 0.75, label: 'Severe', varName: '--severity-4', radius: 11 },
];

/** Resolve a token to its literal colour — MapLibre paint cannot read CSS vars. */
export function severityColors(): string[] {
  return SEVERITY_TIERS.map((t) => cssVar(t.varName));
}

export function unknownColor(): string {
  return cssVar('--severity-unknown');
}

export function repairedColor(): string {
  return cssVar('--color-repaired');
}

export function haloColor(): string {
  return cssVar('--marker-halo');
}

/**
 * Ring for the selected marker.
 *
 * The INVERSE of the halo: near-black on the light theme, cream on the dark
 * one. A selected marker has to be findable at a glance among a hundred others,
 * and thickening a halo that already matches the background does nothing.
 */
export function selectedRingColor(): string {
  return cssVar('--color-text');
}

/** The label for a cluster with no severity score. Also a filter key in the dock. */
export const UNRATED_LABEL = 'Unrated';

export function tierFor(severity: number | null | undefined): SeverityTier | null {
  if (severity === null || severity === undefined || Number.isNaN(severity)) return null;
  let match: SeverityTier | null = null;
  for (const tier of SEVERITY_TIERS) {
    if (severity >= tier.min) match = tier;
  }
  return match;
}

export function severityLabel(severity: number | null | undefined): string {
  return tierFor(severity)?.label ?? UNRATED_LABEL;
}

/**
 * A MapLibre `step` expression mapping severity to colour.
 *
 * `coalesce` is not optional here: `asset_cluster.severity` is nullable, and
 * ST_AsMVT omits null attributes from the tile entirely — the key is simply
 * absent from the feature. A bare `['get','severity']` then feeds null into the
 * step and MapLibre raises an expression error at render time, which surfaces as
 * console spam and miscoloured markers rather than an obvious failure.
 *
 * The sentinel is -1 so a missing value lands below the first tier's 0 bound and
 * paints as "unrated" rather than as "low".
 */
export function severityColorExpression(attribute = 'severity'): ExpressionSpecification {
  const colors = severityColors();
  const stops = SEVERITY_TIERS.flatMap((tier, i) => [tier.min, colors[i] ?? unknownColor()]);
  return [
    'step',
    ['coalesce', ['get', attribute], -1],
    unknownColor(),
    ...stops,
  ] as ExpressionSpecification;
}

/** Radius scales with severity so colour is never the only channel. */
export function severityRadiusExpression(attribute = 'severity'): ExpressionSpecification {
  const stops = SEVERITY_TIERS.flatMap((tier) => [tier.min, tier.radius]);
  return ['step', ['coalesce', ['get', attribute], -1], 4, ...stops] as ExpressionSpecification;
}
