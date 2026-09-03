/**
 * The review queue's state machine. Pure — no DOM, no fetch, no imports but geometry.
 *
 * Kept separate from the view so it can be tested without a browser, and because the
 * two mistakes that matter most here are invisible on screen: a frame that leaves the
 * queue without its work being stored, and a frame that gets signed off without a
 * human ever having looked at it.
 *
 * **A frame's state is DERIVED from the server's fields, never tracked alongside
 * them.** That is what makes a reload recoverable: `boxes_drafted_at` is the draft
 * ledger, so the browser holds no authoritative state it could lose.
 */

import type { DetectionBox, VlmVerdict } from '../types.ts';
import { boxKey, type NormBox } from './geometry.ts';

/** Server truth for one frame. Mirrors `app/models/review.py::ReviewFrameItem`. */
export interface ReviewFrame {
  client_id: string;
  ts: string | null;
  image_url: string;
  device_probability: number | null;
  server_probability: number | null;
  label: number | null;
  note: string | null;
  labeled_by: string | null;
  labeled_at: string | null;
  boxed_at: string | null;
  boxes_drafted_at: string | null;
  human_boxes: NormBox[];
  /**
   * Detector boxes carry more than geometry -- `label` is the class NAME the model
   * assigned, which the overlay renders so class is not conveyed by hue alone.
   *
   * These are `DetectionBox` from types.ts, which is the accurate mirror of
   * app/models/clusters.py::DetectionBox. A local `DetectorBox extends NormBox` used
   * to live here and got both optionalities backwards -- it made `class_id` required
   * and `confidence` optional, where the server has it the other way round. It never
   * bit only because the overlay reads `class_id` for human boxes alone.
   */
  server_boxes: DetectionBox[];
  device_boxes: DetectionBox[];
  vlm_verdict: VlmVerdict | null;
}

export type FrameState = 'unjudged' | 'judged' | 'draft' | 'signed';

export interface Entry {
  item: ReviewFrame;
  /** Local working copy of the human boxes. Box mode only. */
  boxes: NormBox[];
  /** Boxes differ from what the server last acknowledged. */
  dirty: boolean;
  /**
   * Arrived here by Shift+move, Home or End — i.e. displayed but not worked on.
   *
   * Load-bearing: a peeked frame is EXCLUDED from the submit list. Signing off a
   * frame the operator only glanced past would assert "reviewed, genuinely clean"
   * about an image nobody examined.
   */
  peeked: boolean;
  /** The JPEG failed to load. You must not judge a frame you cannot see. */
  imageFailed: boolean;
  /** Last write that did not land, kept so it can be retried and surfaced. */
  writeError: string | null;
}

export type Mode = 'verdict' | 'box';

export function toEntry(item: ReviewFrame): Entry {
  return {
    item,
    boxes: item.human_boxes.map((b) => ({ ...b })),
    dirty: false,
    peeked: false,
    imageFailed: false,
    writeError: null,
  };
}

export function stateOf(entry: Entry): FrameState {
  const { label, boxed_at, boxes_drafted_at } = entry.item;
  if (label === null) return 'unjudged';
  if (boxed_at !== null) return 'signed';
  if (boxes_drafted_at !== null) return 'draft';
  return 'judged';
}

/**
 * Would saving this frame's boxes change anything on the server?
 *
 * Ported from the CLI: without this, browsing back and forth would rewrite the same
 * rows on every keystroke.
 */
export function needsSave(entry: Entry): boolean {
  if (entry.imageFailed) return false;
  const recorded = entry.item.boxes_drafted_at !== null;
  if (!recorded) return true;
  return boxKey(entry.boxes) !== boxKey(entry.item.human_boxes);
}

/**
 * The ids that may be signed off.
 *
 * Three exclusions, each for a different reason, and the server independently
 * enforces the first — `finalize_boxes` refuses a frame with no `boxes_drafted_at`
 * because the exporter would ship it as a background image. This is the client
 * being correct, not the client being the guard.
 */
export function submittableIds(entries: readonly Entry[]): string[] {
  return entries
    .filter((e) => {
      if (e.imageFailed) return false; //   never actually looked at it
      if (e.peeked) return false; //         only glanced past it
      if (stateOf(e) === 'signed') return false; // already done
      // `boxes_drafted_at` is the whole test, and it covers a draft saved this
      // session too: a successful save writes the timestamp back onto the item, so
      // there is no separate session ledger to fall out of step with the server.
      return e.item.boxes_drafted_at !== null;
    })
    .map((e) => e.item.client_id);
}

/** Clamp a cursor for a mode. The two clamps differ, deliberately — see `advance`. */
export function clampCursor(cursor: number, length: number, mode: Mode): number {
  if (length === 0) return 0;
  const max = mode === 'verdict' ? length : length - 1;
  return Math.min(Math.max(cursor, 0), max);
}

/**
 * Where the cursor goes after recording a verdict.
 *
 * `length`, not `length - 1`: verdict mode deliberately allows the cursor to land one
 * PAST the end, which is how "nothing left on this page" is represented rather than
 * sticking on the last frame with no way to tell you are done. Box mode must NOT do
 * this — `current()` would be null and a pending save would be stranded — which is
 * why `clampCursor` takes the mode.
 */
export function advance(cursor: number, length: number): number {
  return Math.min(cursor + 1, length);
}

export interface MoveResult {
  cursor: number;
  /** True when the caller must save the frame it is leaving before moving. */
  save: boolean;
  /** True when the destination should be marked peeked. */
  peek: boolean;
}

/**
 * Resolve a navigation request. Pure: the caller performs the save and applies this.
 *
 * The rules, and why each exists:
 *
 * - **Verdict mode never saves on a move.** The verdict keys are what record; a move
 *   is just a move.
 * - **Box mode saves on every recording move**, so a crash costs nothing.
 * - **Shift peeks**: move without saving and mark the destination, so it cannot later
 *   be signed off as reviewed.
 * - **The save is requested even when the move is a no-op.** At the last frame the
 *   *move* does nothing; the *save* still must happen. Disabling that is the recorded
 *   CLI bug where the final frame "never became a draft, so Submit had no id to sign
 *   off" and it never left the queue.
 */
export function resolveMove(
  cursor: number,
  length: number,
  mode: Mode,
  step: number,
  shift: boolean,
): MoveResult {
  const recording = mode === 'box' && !shift;
  return {
    cursor: clampCursor(cursor + step, length, mode),
    save: recording,
    peek: !recording && mode === 'box',
  };
}

/** Home / End: a jump crosses frames it never displayed, so it records none of them. */
export function resolveJump(to: 'start' | 'end', length: number, mode: Mode): MoveResult {
  return {
    cursor: clampCursor(to === 'start' ? 0 : length - 1, length, mode),
    save: false,
    peek: mode === 'box',
  };
}

/**
 * Fold a submit response back into the queue.
 *
 * `skipped_*` are surfaced rather than discarded: an operator who pressed submit on
 * 50 frames and had 3 refused needs to know which, and why, before they discover it
 * in a training run.
 */
export interface SubmitOutcome {
  finalized: number;
  already_finalized: number;
  skipped_unjudged: string[];
  skipped_undrafted: string[];
}

export function applySubmit(entries: Entry[], sent: readonly string[], out: SubmitOutcome): void {
  const refused = new Set([...out.skipped_unjudged, ...out.skipped_undrafted]);
  const stamp = new Date().toISOString();
  for (const e of entries) {
    if (!sent.includes(e.item.client_id)) continue;
    if (refused.has(e.item.client_id)) continue;
    e.item.boxed_at = e.item.boxed_at ?? stamp;
  }
}

/** The progress line. Wording preserved from the CLI, where it was tuned in use. */
export function progressLine(opts: {
  drafts: number;
  queueLength: number;
  reviewMode: boolean;
  mode: Mode;
}): string {
  const { drafts, queueLength, reviewMode, mode } = opts;
  if (queueLength === 0) return 'queue empty';
  // Verdict mode has no submit step -- the verdict keys are what record. Saying
  // "left to submit" there sends the operator looking for a button that is not
  // there, and is only correct in box mode.
  if (mode === 'verdict') {
    return reviewMode
      ? `${queueLength} judged frame(s) — review mode`
      : `${queueLength} left to judge`;
  }
  if (drafts > 0) {
    return drafts === queueLength
      ? `every frame drafted — press s to submit all ${drafts}`
      : `${drafts} draft(s) not submitted — press s to sign them off`;
  }
  if (reviewMode) return `${queueLength} submitted frame(s) — review mode`;
  return `${queueLength} left to submit`;
}
