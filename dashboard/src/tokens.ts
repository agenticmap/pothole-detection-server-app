/**
 * Reading design tokens from JavaScript.
 *
 * Everything visual is a CSS custom property in tokens.css, which is what makes
 * dark mode and per-municipality white-labelling a single-block change. Two
 * consumers cannot read CSS, though, and both live in the map:
 *
 *   - MapLibre paint values (severity.ts) — the style spec has no notion of
 *     `var()`, so tier colours must be resolved to literal hex.
 *   - The basemap flavor (map/basemap.ts) — @protomaps/basemaps takes a plain
 *     object of colour strings.
 *
 * Both resolve at call time against `document.documentElement`, so a caller that
 * flips `data-theme` first and reads second gets the new palette. theme.ts sets
 * the attribute synchronously for exactly this reason.
 */

/** Resolve one custom property to its computed literal value. */
export function cssVar(name: string): string {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}
