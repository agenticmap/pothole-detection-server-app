/**
 * Would the public read path serve this cluster?
 *
 * Top-level rather than inside `map/` because three surfaces have to agree about
 * the word "corroborated" — the map draws an uncorroborated cluster as a ring, the
 * panel badges it as a candidate, and the dock's KPI counts it — and a panel
 * importing from `map/` to find out would be the wrong dependency.
 *
 * **Not to be confused with `panel/corroboration.ts`**, which turns the same idea
 * into a sentence for one cluster. That file writes prose; this one is the
 * predicate. One word, two jobs.
 *
 * ## Why the numbers live here at all
 *
 * The tile endpoint deliberately applies NO corroboration filter — an operator
 * triages candidates, so `/clusters/*` serves everything — while
 * `/api/v1/potholes` applies both floors (`cluster_query_service._FILTER`). The
 * client therefore has to draw the distinction itself, which means duplicating two
 * numbers across a language boundary. `corroboration.spec.ts` reads `app/config.py`
 * and fails if they ever drift apart.
 */

/** `app/config.py::cluster_min_distinct_devices`. */
export const MIN_DISTINCT_DEVICES = 2;
/** `app/config.py::cluster_min_distinct_passes`. */
export const MIN_DISTINCT_PASSES = 3;

/**
 * The publication rule, verbatim: devices **OR** passes.
 *
 * It is an OR because the source paper's own validation was one phone driven on
 * five different days — requiring two devices as well would score a single-vehicle
 * survey campaign at zero, which is precisely the campaign this project has run.
 */
export function isCorroborated(distinctDevices: number, distinctPasses: number): boolean {
  return distinctDevices >= MIN_DISTINCT_DEVICES || distinctPasses >= MIN_DISTINCT_PASSES;
}
