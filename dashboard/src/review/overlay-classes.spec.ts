import { describe, expect, it } from 'vitest';

import { boxClassName, detectorBoxLabel } from './overlay.ts';

describe('boxClassName', () => {
  it('gives a human box its class hue', () => {
    expect(boxClassName('human', 3, false)).toContain('review-box-c3');
    expect(boxClassName('human', 0, false)).toContain('review-box-c0');
  });

  it('never gives a detector box a class hue', () => {
    // The rule was prose-only in overlay.ts ("they must never look like a human's").
    // The five class colours are the human's vocabulary; a detector box borrowing one
    // would tell the labeller the model had already assigned the class they are being
    // asked to assign, which is the anchoring Phase 2.7b measured as harmful.
    for (const kind of ['server', 'device'] as const) {
      const cls = boxClassName(kind, 3, false);
      expect(cls).toContain(`review-box-${kind}`);
      expect(cls).not.toMatch(/review-box-c\d/);
    }
  });

  it('tolerates a missing or null class_id without emitting a junk class', () => {
    // DetectionBox.class_id is optional AND nullable -- that is the server's shape.
    // The old code interpolated it unguarded, so a detector box could have produced
    // `review-box-cundefined`.
    expect(boxClassName('human', null, false)).not.toContain('review-box-c');
    expect(boxClassName('human', undefined, false)).not.toContain('review-box-c');
  });

  it('marks selection without dropping the class', () => {
    const cls = boxClassName('human', 2, true);
    expect(cls).toContain('is-selected');
    expect(cls).toContain('review-box-c2');
  });

  it('always carries the base class', () => {
    expect(boxClassName('device', null, false).split(' ')).toContain('review-box');
  });
});

describe('detectorBoxLabel', () => {
  it('names the source and the class and the score', () => {
    expect(detectorBoxLabel('srv', { label: 'pothole', confidence: 0.617 })).toBe('srv pothole 0.62');
    expect(detectorBoxLabel('dev', { label: 'pothole', confidence: 0.48 })).toBe('dev pothole 0.48');
  });

  it('prints a zero confidence rather than hiding it', () => {
    // 39% of this corpus scores exactly 0.0, and 0.0 is a real measurement here --
    // a truthiness guard would have made the commonest case render as no score.
    expect(detectorBoxLabel('srv', { label: 'pothole', confidence: 0 })).toBe('srv pothole 0.00');
  });

  it('leaves no double space when the class name is missing', () => {
    expect(detectorBoxLabel('dev', { confidence: 0.5 })).toBe('dev 0.50');
    expect(detectorBoxLabel('dev', { label: null, confidence: 0.5 })).toBe('dev 0.50');
  });

  it('degrades to the prefix alone rather than printing NaN', () => {
    expect(detectorBoxLabel('srv', {})).toBe('srv');
    expect(detectorBoxLabel('srv', { confidence: Number.NaN })).toBe('srv');
  });
});
