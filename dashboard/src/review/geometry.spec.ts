/**
 * Box geometry. These are the coordinate rules whose violations are invisible.
 *
 * A wrong convention here does not crash — it stores boxes offset by half their size
 * and trains a detector on them. `tests/test_box_export.py` exists server-side for the
 * same reason; this is its client-side half.
 */

import { describe, expect, it } from 'vitest';

import {
  boxKey,
  clamp01,
  dragToBox,
  EDGE_SLACK,
  hitTest,
  isThin,
  isValidBox,
  MIN_DRAG_PX,
  type NormBox,
  pointerToPx,
} from './geometry.ts';

const RECT = { left: 100, top: 50, width: 800, height: 600 };
const box = (o: Partial<NormBox> = {}): NormBox => ({
  class_id: 0,
  x: 0.1,
  y: 0.2,
  w: 0.3,
  h: 0.4,
  ...o,
});

describe('pointerToPx', () => {
  it('is relative to the image rect, not the viewport', () => {
    expect(pointerToPx(150, 100, RECT)).toEqual({ x: 50, y: 50 });
  });

  it('clamps a pointer dragged outside the image', () => {
    expect(pointerToPx(5000, 5000, RECT)).toEqual({ x: 800, y: 600 });
    expect(pointerToPx(0, 0, RECT)).toEqual({ x: 0, y: 0 });
  });
});

describe('dragToBox', () => {
  it('round-trips pixels to normalized coordinates', () => {
    const b = dragToBox({ x: 80, y: 60 }, { x: 240, y: 180 }, RECT, 0);
    expect(b).not.toBeNull();
    expect(b!.x).toBeCloseTo(0.1);
    expect(b!.y).toBeCloseTo(0.1);
    expect(b!.w).toBeCloseTo(0.2);
    expect(b!.h).toBeCloseTo(0.2);
  });

  it('accepts a drag in any direction', () => {
    const down = dragToBox({ x: 80, y: 60 }, { x: 240, y: 180 }, RECT, 0);
    const up = dragToBox({ x: 240, y: 180 }, { x: 80, y: 60 }, RECT, 0);
    expect(up).toEqual(down);
  });

  it('treats a stray click as a click, not a zero-area box', () => {
    const tiny = MIN_DRAG_PX - 1;
    expect(dragToBox({ x: 80, y: 60 }, { x: 80 + tiny, y: 60 + tiny }, RECT, 0)).toBeNull();
  });

  it('rejects a drag against an unrendered image', () => {
    expect(dragToBox({ x: 0, y: 0 }, { x: 50, y: 50 }, { width: 0, height: 0 }, 0)).toBeNull();
  });

  it('carries the active class through', () => {
    expect(dragToBox({ x: 0, y: 0 }, { x: 100, y: 100 }, RECT, 3)?.class_id).toBe(3);
  });
});

describe('isValidBox — mirrors review_service.validate_boxes', () => {
  it('accepts a box drawn flush to the right edge', () => {
    // The 1.0001 slack in migrations/013 exists for exactly this: it absorbs the
    // browser's pixel -> fraction rounding. A hard 1.0 would reject a pothole boxed
    // at the shoulder, which is where they sit.
    expect(isValidBox(box({ x: 0.5, w: 0.50005 }), 5)).toBe(true);
    expect(0.5 + 0.50005).toBeLessThanOrEqual(EDGE_SLACK);
  });

  it('rejects a box genuinely past the edge', () => {
    expect(isValidBox(box({ x: 0.5, w: 0.51 }), 5)).toBe(false);
  });

  it('rejects zero area', () => {
    expect(isValidBox(box({ w: 0 }), 5)).toBe(false);
    expect(isValidBox(box({ h: 0 }), 5)).toBe(false);
  });

  it('rejects an origin outside the frame', () => {
    expect(isValidBox(box({ x: -0.1 }), 5)).toBe(false);
    expect(isValidBox(box({ y: 1.5 }), 5)).toBe(false);
  });

  it('rejects an out-of-range class', () => {
    expect(isValidBox(box({ class_id: 5 }), 5)).toBe(false);
    expect(isValidBox(box({ class_id: -1 }), 5)).toBe(false);
    expect(isValidBox(box({ class_id: 4 }), 5)).toBe(true);
  });

  it('every box the drawing path can produce is valid', () => {
    // The drag is clamped to the rect, so the product must always satisfy the
    // server. If this ever fails, drawing produces 400s at the edges.
    for (const [sx, sy, ex, ey] of [
      [0, 0, 800, 600],
      [799, 599, 0, 0],
      [400, 300, 800, 600],
      [0, 0, 20, 20],
    ] as const) {
      const b = dragToBox({ x: sx, y: sy }, { x: ex, y: ey }, RECT, 0);
      if (b) expect(isValidBox(b, 5)).toBe(true);
    }
  });
});

describe('hitTest', () => {
  it('finds the box under a point', () => {
    expect(hitTest([box()], 0.2, 0.3)).toBe(0);
    expect(hitTest([box()], 0.9, 0.9)).toBe(-1);
  });

  it('prefers the last drawn, so a small box on a large one stays reachable', () => {
    const big = box({ x: 0, y: 0, w: 1, h: 1 });
    const small = box({ x: 0.4, y: 0.4, w: 0.1, h: 0.1 });
    expect(hitTest([big, small], 0.45, 0.45)).toBe(1);
  });

  it('is empty-safe', () => {
    expect(hitTest([], 0.5, 0.5)).toBe(-1);
  });
});

describe('boxKey', () => {
  it('is stable across key order and object identity', () => {
    const a: NormBox = { class_id: 1, x: 0.1, y: 0.2, w: 0.3, h: 0.4 };
    const b = { h: 0.4, w: 0.3, y: 0.2, x: 0.1, class_id: 1 } as NormBox;
    expect(boxKey([a])).toBe(boxKey([b]));
  });

  it('distinguishes a moved box', () => {
    expect(boxKey([box()])).not.toBe(boxKey([box({ x: 0.11 })]));
  });

  it('distinguishes a reclassified box', () => {
    expect(boxKey([box()])).not.toBe(boxKey([box({ class_id: 1 })]));
  });

  it('treats null, undefined and empty alike — all mean "no boxes"', () => {
    expect(boxKey(null)).toBe('');
    expect(boxKey(undefined)).toBe('');
    expect(boxKey([])).toBe('');
  });

  it('ignores differences below the wire precision', () => {
    // Six decimal places is what the wire and the CHECK constraint agree on. A
    // difference finer than that is float noise, not an edit, and must not trigger
    // a rewrite of every row on every keystroke.
    expect(boxKey([box({ x: 0.1 })])).toBe(boxKey([box({ x: 0.1 + 1e-9 })]));
  });
});

describe('isThin — mirrors app/detection/classes.py::is_thin', () => {
  it('flags a sliver in either orientation', () => {
    expect(isThin(0.6, 0.01, 6)).toBe(true);
    expect(isThin(0.01, 0.6, 6)).toBe(true);
  });

  it('leaves a compact box alone', () => {
    expect(isThin(0.2, 0.2, 6)).toBe(false);
  });

  it('is degenerate-safe', () => {
    expect(isThin(0, 0.5, 6)).toBe(false);
  });

  it('matches the server exactly at the boundary', () => {
    // app/detection/classes.py uses `long / short > ratio`, strictly. A `>=` here
    // would warn on a box the server considers fine, and that file's docstring
    // says the two must agree.
    expect(isThin(0.6, 0.1, 6)).toBe(false); // ratio exactly 6.0
    expect(isThin(0.61, 0.1, 6)).toBe(true); // just over
  });
});

describe('clamp01', () => {
  it('bounds to the unit interval', () => {
    expect(clamp01(-1)).toBe(0);
    expect(clamp01(2)).toBe(1);
    expect(clamp01(0.5)).toBe(0.5);
  });
});
