/**
 * What a camera-frame map marker says about itself. Pure: tile properties in, rows
 * and a status line out.
 *
 * Extracted from `map.ts::framePopupContent` so it can be tested at all. The popup
 * was a `props -> HTMLElement` function with the whole decision tree inline, which
 * meant the two rules that actually matter — that an UNSCORED frame reads as unscored
 * rather than as scored-zero, and that unscored is checked BEFORE unpaired — were
 * only verifiable by opening a browser and clicking a dot at zoom 15.
 *
 * The tile ships `server_box_count` rather than the boxes themselves, deliberately:
 * `tile_service.py` notes that frame-relative box geometry is meaningless in map
 * space. So this surface can say how many boxes were found and not where.
 */

/** A `[term, value]` pair for the popup's definition list. */
export type Fact = [string, string];

export interface FrameStatus {
  text: string;
  /** True when the frame contributed nothing — rendered as the outlier flag. */
  severe: boolean;
}

const KNOWN_PROPS = new Set([
  'client_id',
  'device_probability',
  'server_probability',
  'server_model_id',
  'server_box_count',
  'detected',
  'paired',
  'is_primary',
  'fused_confidence',
  'ts_epoch',
]);

function num(v: unknown, digits: number): string | null {
  return typeof v === 'number' && Number.isFinite(v) ? v.toFixed(digits) : null;
}

/**
 * The fusion outcome, which leads the popup.
 *
 * **Order is load-bearing.** A frame that is neither scored nor paired must read as
 * "not yet scored" — the more fundamental fact, and the one that tells the operator
 * the pipeline has not run rather than that it ran and found nothing to match.
 * Checking `paired` first would report a consequence as a cause.
 */
export function frameStatus(props: Record<string, unknown>): FrameStatus {
  if (props['detected'] !== true) {
    return {
      text: 'Not yet scored — the detection worker has not reached this frame.',
      severe: true,
    };
  }
  if (props['paired'] !== true) {
    return {
      text: 'Scored but unpaired — no sensor event matched, so it reached no cluster.',
      severe: true,
    };
  }
  return { text: 'Paired with a sensor event and fused.', severe: false };
}

/**
 * Every row the popup shows, in order.
 *
 * Both score rows are ALWAYS present. The previous version omitted the on-device row
 * when the value was null, so the popup's shape changed between frames and two frames
 * with different provenance could look the same. An em dash is information; a missing
 * row is not.
 */
export function frameFacts(props: Record<string, unknown>): Fact[] {
  const rows: Fact[] = [];

  // `?? 'not scored'` rather than a truthiness check: 0.000 is the commonest server
  // score in this corpus and means the detector found nothing, which is a result.
  rows.push(['Server p', num(props['server_probability'], 3) ?? 'not scored']);
  rows.push(['On-device p', num(props['device_probability'], 3) ?? '—']);

  const model = props['server_model_id'];
  if (typeof model === 'string' && model) rows.push(['Model', model]);

  const boxes = props['server_box_count'];
  // "Server boxes", not "Boxes found": the count is server_box_count specifically,
  // and a device count exists too, so the old label claimed more than it delivered.
  if (typeof boxes === 'number') rows.push(['Server boxes', String(boxes)]);

  const fused = num(props['fused_confidence'], 3);
  if (fused !== null) {
    rows.push(['Fused confidence', props['is_primary'] === true ? `${fused} (primary)` : fused]);
  }

  const ts = props['ts_epoch'];
  if (typeof ts === 'number' && Number.isFinite(ts)) {
    rows.push(['Captured', `${new Date(ts * 1000).toISOString().replace('T', ' ').slice(0, 19)}Z`]);
  }

  // Anything the tile grew that this function has not been taught about, rather than
  // silently dropped. A new column is then visible on the very first click.
  for (const [key, value] of Object.entries(props)) {
    if (!KNOWN_PROPS.has(key)) rows.push([key, String(value)]);
  }

  return rows;
}
