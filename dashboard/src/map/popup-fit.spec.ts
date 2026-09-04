import { describe, expect, it } from 'vitest';
import { needsPan, popupPanOffset, type Rect } from './popup-fit';

/** The map pane measured in the browser: topbar above it, a little margin below. */
const MAP: Rect = { top: 66, bottom: 768, left: 112, right: 1416 };

const rect = (top: number, height: number, left = 1039, width = 242): Rect => ({
  top,
  bottom: top + height,
  left,
  right: left + width,
});

describe('popupPanOffset', () => {
  it('does not pan a popup that already fits', () => {
    expect(popupPanOffset(rect(200, 300), MAP)).toEqual({ x: 0, y: 0 });
  });

  it('pans up the exact overflow when the popup runs off the bottom', () => {
    // The reproduced defect: a 544px frame popup opened at y=292 ended at 836,
    // 68px past the map's bottom edge of 768.
    const offset = popupPanOffset(rect(292, 544), MAP);
    // 836 - (768 - 12) = 80. Positive y moves features up, which is what we want.
    expect(offset.y).toBe(80);
    expect(offset.x).toBe(0);
  });

  it('after panning by the returned offset, the popup is inside the map', () => {
    const popup = rect(292, 544);
    const { y } = popupPanOffset(popup, MAP);
    const moved = { ...popup, top: popup.top - y, bottom: popup.bottom - y };
    expect(moved.bottom).toBeLessThanOrEqual(MAP.bottom);
    expect(moved.top).toBeGreaterThanOrEqual(MAP.top);
  });

  it('pans down when the popup runs off the top', () => {
    const offset = popupPanOffset(rect(20, 200), MAP);
    // 20 - (66 + 12) = -58: negative y moves features down the screen.
    expect(offset.y).toBe(-58);
  });

  it('aligns the top when the popup is taller than the map can ever show', () => {
    // No pan can fully reveal it, so favour the top — title, photograph, close button.
    const offset = popupPanOffset(rect(300, 900), MAP);
    expect(offset.y).toBe(300 - (66 + 12));
    const moved = 300 - offset.y;
    expect(moved).toBe(78);
  });

  it('pans horizontally when the popup overhangs the right edge', () => {
    const offset = popupPanOffset(rect(300, 200, 1300, 242), MAP);
    expect(offset.x).toBe(1542 - (1416 - 12));
    expect(offset.y).toBe(0);
  });

  it('pans horizontally when the popup overhangs the left edge, under the dock', () => {
    const offset = popupPanOffset(rect(300, 200, 60, 242), MAP);
    expect(offset.x).toBe(60 - (112 + 12));
    expect(offset.x).toBeLessThan(0);
  });

  it('corrects both axes at once', () => {
    const offset = popupPanOffset(rect(600, 300, 1350, 242), MAP);
    expect(offset.y).toBeGreaterThan(0);
    expect(offset.x).toBeGreaterThan(0);
  });
});

describe('needsPan', () => {
  it('ignores sub-pixel drift rather than easing the map for nothing', () => {
    expect(needsPan({ x: 0, y: 0 })).toBe(false);
    expect(needsPan({ x: 1, y: -2 })).toBe(false);
  });

  it('reports a real overflow', () => {
    expect(needsPan({ x: 0, y: 80 })).toBe(true);
    expect(needsPan({ x: -58, y: 0 })).toBe(true);
  });
});
