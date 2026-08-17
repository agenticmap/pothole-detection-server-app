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
 * Tier boundaries. `asset_cluster.severity` is an IRI-style figure produced by
 * app/sensor_model — unbounded in principle, in practice single digits. Tiers are
 * a first cut and should be recalibrated against real drive data before a pilot.
 */
export const SEVERITY_TIERS: readonly SeverityTier[] = [
  { min: 0, label: 'Low', varName: '--severity-1', radius: 5 },
  { min: 1.5, label: 'Moderate', varName: '--severity-2', radius: 7 },
  { min: 3, label: 'High', varName: '--severity-3', radius: 9 },
  { min: 5, label: 'Severe', varName: '--severity-4', radius: 11 },
];

function cssVar(name: string): string {
  const value = getComputedStyle(document.documentElement).getPropertyValue(name);
  return value.trim();
}

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

export function tierFor(severity: number | null | undefined): SeverityTier | null {
  if (severity === null || severity === undefined || Number.isNaN(severity)) return null;
  let match: SeverityTier | null = null;
  for (const tier of SEVERITY_TIERS) {
    if (severity >= tier.min) match = tier;
  }
  return match;
}

export function severityLabel(severity: number | null | undefined): string {
  return tierFor(severity)?.label ?? 'Unrated';
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
