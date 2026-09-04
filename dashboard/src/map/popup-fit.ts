/**
 * Keep an open popup inside the map.
 *
 * MapLibre picks a popup's anchor from which side of the marker has more room, but
 * it never shrinks or repositions once that choice is made. The camera-frame popup
 * carries a photograph and runs to ~540px, which fits on NEITHER side of a marker
 * in the middle of a 700px map — so it opened downward and had its "Open full size"
 * button cut off by the map's bottom edge.
 *
 * Capping the height alone would fix the clipping by making every popup small. This
 * pans the map instead, so the popup keeps its size and the operator keeps the
 * photograph. The height cap in styles.css stays as the backstop for a window too
 * short for any pan to help.
 *
 * Pure and rect-based so it can be specced in the node environment — there is no
 * jsdom here, and a function that took a Map and a Popup could not be tested at all.
 */

export interface Rect {
  readonly top: number;
  readonly bottom: number;
  readonly left: number;
  readonly right: number;
}

/**
 * How far to pan so `popup` sits inside `map` with `margin` to spare.
 *
 * Returns MapLibre `panBy` offsets: **positive y moves features UP the screen**,
 * positive x moves them LEFT. That is the opposite of the overflow direction, which
 * is why each branch negates.
 *
 * When the popup is larger than the viewport on an axis, no pan makes it fully
 * visible. Prefer showing its top-left — the title, the photograph and the close
 * button live there, and the height cap makes the remainder scrollable.
 */
export function popupPanOffset(popup: Rect, map: Rect, margin = 12): { x: number; y: number } {
  let y = 0;
  const availableHeight = map.bottom - map.top - margin * 2;
  if (popup.bottom - popup.top > availableHeight) {
    // Too tall to fit: align the top rather than centring, so the close button and
    // the image stay on screen and the overflow falls off the bottom where the
    // content region is already scrollable.
    y = popup.top - (map.top + margin);
  } else if (popup.bottom > map.bottom - margin) {
    y = popup.bottom - (map.bottom - margin);
  } else if (popup.top < map.top + margin) {
    y = popup.top - (map.top + margin);
  }

  let x = 0;
  const availableWidth = map.right - map.left - margin * 2;
  if (popup.right - popup.left > availableWidth) {
    x = popup.left - (map.left + margin);
  } else if (popup.right > map.right - margin) {
    x = popup.right - (map.right - margin);
  } else if (popup.left < map.left + margin) {
    x = popup.left - (map.left + margin);
  }

  return { x, y };
}

/** Below this, a pan is visual noise rather than a fix. */
export const PAN_EPSILON_PX = 2;

export function needsPan(offset: { x: number; y: number }): boolean {
  return Math.abs(offset.x) > PAN_EPSILON_PX || Math.abs(offset.y) > PAN_EPSILON_PX;
}
