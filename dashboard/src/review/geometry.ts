/**
 * Box geometry for the review surface. Pure — no DOM, no imports.
 *
 * ONE coordinate convention, everywhere: **normalized 0..1, corner-origin,
 * full-frame**. That is what `frame_box` stores, what `device_detections` and
 * `server_detections` carry, and what the API sends and accepts. The centre-origin
 * YOLO form exists only inside `scripts/export_labeled_frames.py`, converted once
 * with a round-trip test. A second convention here would not crash — it would
 * silently train on boxes offset by half their size.
 *
 * Pixels enter and leave only at the edges of this module, via the image's own
 * bounding rect. Nothing downstream ever stores a pixel.
 */

/** A box as stored and transmitted. */
export interface NormBox {
  class_id: number;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** The rendered size of the image, from `getBoundingClientRect()`. */
export interface Rect {
  width: number;
  height: number;
}

/**
 * A drag shorter than this on either axis is a click, not a box.
 *
 * The database would reject a zero-area box anyway (`frame_box`'s CHECK requires
 * `w > 0 AND h > 0`); catching it here keeps the failure out of the operator's way
 * when they meant to select rather than draw.
 */
export const MIN_DRAG_PX = 6;

/**
 * Slack on the right and bottom edges, matching `frame_box_within_frame` in
 * migrations/013.
 *
 * It absorbs float rounding on the browser's pixel -> fraction conversion. A hard
 * 1.0 would reject a box drawn flush to the frame edge, which is exactly where a
 * pothole at the shoulder sits.
 */
export const EDGE_SLACK = 1.0001;

export function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

export function clamp01(value: number): number {
  return clamp(value, 0, 1);
}

/**
 * Pointer position relative to the image, in pixels, clamped to it.
 *
 * Takes the rect from `getBoundingClientRect()` and NEVER `offsetX`/`offsetY`.
 * Those are relative to whatever element is under the cursor — an existing box,
 * usually — which would silently shift every rectangle drawn over another one.
 * This is ported from the CLI, comment and all, because it was a real bug there.
 */
export function pointerToPx(
  clientX: number,
  clientY: number,
  rect: { left: number; top: number; width: number; height: number },
): { x: number; y: number } {
  return {
    x: clamp(clientX - rect.left, 0, rect.width),
    y: clamp(clientY - rect.top, 0, rect.height),
  };
}

/**
 * Turn a completed drag into a normalized box, or null if it was a click.
 *
 * Accepts the two corners in any order — dragging up-left is as valid as
 * down-right, and an operator boxing a pothole at the bottom of the frame
 * routinely does.
 */
export function dragToBox(
  start: { x: number; y: number },
  end: { x: number; y: number },
  rect: Rect,
  classId: number,
): NormBox | null {
  const x0 = Math.min(start.x, end.x);
  const y0 = Math.min(start.y, end.y);
  const w = Math.abs(end.x - start.x);
  const h = Math.abs(end.y - start.y);
  if (w < MIN_DRAG_PX || h < MIN_DRAG_PX) return null;
  if (rect.width <= 0 || rect.height <= 0) return null;
  return {
    class_id: classId,
    x: x0 / rect.width,
    y: y0 / rect.height,
    w: w / rect.width,
    h: h / rect.height,
  };
}

/** Would the server accept this box? Mirrors `review_service.validate_boxes`. */
export function isValidBox(box: NormBox, classCount: number): boolean {
  return (
    Number.isInteger(box.class_id) &&
    box.class_id >= 0 &&
    box.class_id < classCount &&
    box.x >= 0 &&
    box.x <= 1 &&
    box.y >= 0 &&
    box.y <= 1 &&
    box.w > 0 &&
    box.w <= 1 &&
    box.h > 0 &&
    box.h <= 1 &&
    box.x + box.w <= EDGE_SLACK &&
    box.y + box.h <= EDGE_SLACK
  );
}

/**
 * Which box is under a point, topmost first, or -1.
 *
 * Last-drawn wins, so a small box drawn on top of a large one stays selectable —
 * the reverse would make it unreachable.
 */
export function hitTest(boxes: readonly NormBox[], nx: number, ny: number): number {
  for (let i = boxes.length - 1; i >= 0; i--) {
    const b = boxes[i];
    if (!b) continue;
    if (nx >= b.x && nx <= b.x + b.w && ny >= b.y && ny <= b.y + b.h) return i;
  }
  return -1;
}

/**
 * A stable fingerprint of a box set, used to skip a save that would change nothing.
 *
 * Ported verbatim from the CLI: without it, browsing back and forth would rewrite
 * the same rows on every keystroke. Six decimal places because that is the
 * precision the wire and the CHECK constraint agree on.
 */
export function boxKey(boxes: readonly NormBox[] | null | undefined): string {
  return (boxes ?? [])
    .map((b) =>
      [b.class_id, b.x.toFixed(6), b.y.toFixed(6), b.w.toFixed(6), b.h.toFixed(6)].join(','),
    )
    .join(';');
}

/**
 * Is this box a sliver? Mirrors `app/detection/classes.py::is_thin`.
 *
 * Only meaningful for region classes (cracks): box regions, not lines — a sliver
 * is mostly undamaged asphalt. A warning, never a refusal.
 */
export function isThin(w: number, h: number, ratio: number): boolean {
  if (w <= 0 || h <= 0) return false;
  // `>`, not `>=`, to match app/detection/classes.py::is_thin exactly. Its docstring
  // says the rule is "shared by the labelling UI and the save path so the operator's
  // warning and the server's log agree on what counts as thin" — at a ratio of
  // exactly 6.0 a `>=` here would warn where the server would not.
  const short = Math.min(w, h);
  const long = Math.max(w, h);
  return long / short > ratio;
}
