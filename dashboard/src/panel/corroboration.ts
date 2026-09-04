/**
 * Turning `distinct_passes` and `member_span_s` into a sentence an operator can act on.
 *
 * Pure — no DOM, no imports — so it can be tested without jsdom, and because the
 * judgement here is the part worth pinning down. `migrations/015_cluster_passes.sql`
 * calls `member_span_s` "the diagnostic that exposed the problem in the first place":
 * the integration round found the pipeline had produced zero corroborated defects, and
 * the tell was that every cluster's members arrived within seconds of each other — one
 * car driving past once, not three cars agreeing.
 *
 * Rendering that as a bare float would bury it. "1 pass" alone reads like a
 * measurement; "all within 12 s — one drive-past" reads like what it is, which is a
 * warning that nothing has confirmed this defect yet.
 *
 * A cluster with two or more passes needs no note: the `Corroborating passes` field
 * already says so, and repeating it would be noise on the row that matters least.
 */

/** Seconds as the coarsest unit that still reads honestly. */
export function formatSpan(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return '—';
  if (seconds < 60) return `${Math.round(seconds)} s`;
  if (seconds < 3600) return `${Math.round(seconds / 60)} min`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h`;
  const days = seconds / 86400;
  // One decimal below ten days, because "1 day" and "9.5 days" are different claims
  // about corroboration and rounding both to integers hides that.
  return days < 10 ? `${days.toFixed(1)} days` : `${Math.round(days)} days`;
}

/**
 * The corroboration note, or null when there is nothing useful to add.
 *
 * `passes >= 2` returns null deliberately — that is the corroborated case and the
 * field above already reports it.
 */
export function spanNoteText(
  passes: number,
  spanS: number | null,
  observationCount?: number,
): string | null {
  if (passes >= 2) return null;
  // A single reading first, because every branch below describes a SET of
  // observations and none of them is true of one. "All observations within 0 s"
  // is a statement about a set of one, and it was what the panel showed for 163
  // of the corpus's 204 clusters -- forming one takes a single admitted reading
  // (cluster_min_points = 1), so this is the common case, not the edge case.
  if (observationCount === 1) {
    return 'One reading, on one pass. This is a candidate, not a confirmed defect.';
  }
  if (spanS === null || !Number.isFinite(spanS)) {
    return 'One pass — nothing has corroborated this defect yet.';
  }
  // A minute is the generous reading of "one drive-past": at 30 km/h a car covers
  // 500 m in that time, so anything tighter is certainly a single traversal.
  if (spanS < 60) {
    return `All observations within ${formatSpan(spanS)} — one drive-past, not repeat corroboration.`;
  }
  return `One pass spanning ${formatSpan(spanS)} — no repeat coverage yet.`;
}
