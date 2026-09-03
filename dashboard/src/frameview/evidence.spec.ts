import { describe, expect, it } from 'vitest';

import type { DetectionBox, VlmVerdict } from '../types.ts';
import {
  type FrameEvidence,
  formatProb,
  notScoredNote,
  overlayBoxesFor,
  scoreLines,
  vlmSummary,
} from './evidence.ts';

function frame(over: Partial<FrameEvidence> = {}): FrameEvidence {
  return {
    device_probability: 0.48,
    server_probability: 0.62,
    detected_at: '2026-08-20T14:02:00Z',
    server_boxes: [],
    device_boxes: [],
    vlm_verdict: null,
    ...over,
  };
}

const box = (over: Partial<DetectionBox> = {}): DetectionBox => ({
  x: 0.1,
  y: 0.2,
  w: 0.3,
  h: 0.4,
  confidence: 0.55,
  ...over,
});

describe('formatProb', () => {
  it('renders zero as a score, not as absent', () => {
    // 2,183 of 5,615 scored frames in this corpus are exactly 0.0. A truthiness
    // guard would erase the majority case and make "found nothing" look like
    // "never looked".
    expect(formatProb(0)).toBe('0.00');
  });

  it('renders a real absence as an em dash', () => {
    expect(formatProb(null)).toBe('—');
    expect(formatProb(undefined)).toBe('—');
    expect(formatProb(Number.NaN)).toBe('—');
  });

  it('fixes two decimals', () => {
    expect(formatProb(0.617)).toBe('0.62');
  });
});

describe('scoreLines', () => {
  it('always returns both rows so provenance is never ambiguous', () => {
    const lines = scoreLines(frame());
    expect(lines.map((l) => l.label)).toEqual(['srv', 'dev']);
    expect(lines.map((l) => l.value)).toEqual(['0.62', '0.48']);
  });

  it('keeps both rows when one score is missing', () => {
    // The old panel collapsed these with `??`, so an unscored frame showing the
    // phone's number was indistinguishable from a server-scored one.
    const lines = scoreLines(frame({ server_probability: null }));
    expect(lines).toHaveLength(2);
    expect(lines[0]?.value).toBe('—');
    expect(lines[1]?.value).toBe('0.48');
  });

  it('warns in the server row that a VLM verdict makes the score a blend', () => {
    const withVlm = scoreLines(frame({ vlm_verdict: verdict() }));
    expect(withVlm[0]?.title).toContain('BLEND');
    expect(scoreLines(frame())[0]?.title).not.toContain('BLEND');
  });
});

describe('notScoredNote', () => {
  it('keys on detected_at, not on the probability', () => {
    // A frame the detector ran on and found nothing in scores 0.0 and IS scored.
    expect(notScoredNote(frame({ server_probability: 0 }))).toBeNull();
    expect(notScoredNote(frame({ detected_at: null }))).toBe('not yet scored');
  });

  it('still says "not yet scored" when a device probability exists', () => {
    // This is the exact case the new drive produced: 2,000 frames with a phone score
    // and no server score at all.
    expect(notScoredNote(frame({ detected_at: null, server_probability: null }))).toBe(
      'not yet scored',
    );
  });

  it('says nothing for a shape that does not carry detected_at', () => {
    const { detected_at: _omitted, ...rest } = frame();
    expect(notScoredNote(rest as FrameEvidence)).toBeNull();
  });
});

describe('overlayBoxesFor', () => {
  it('omits a set that is toggled off', () => {
    const f = frame({ server_boxes: [box()], device_boxes: [box(), box()] });
    expect(overlayBoxesFor(f, { server: true, device: false })).toHaveLength(1);
    expect(overlayBoxesFor(f, { server: false, device: true })).toHaveLength(2);
    expect(overlayBoxesFor(f, { server: false, device: false })).toHaveLength(0);
  });

  it('tags each box with its source, in both the kind and the label', () => {
    const f = frame({
      server_boxes: [box({ label: 'pothole', confidence: 0.62 })],
      device_boxes: [box({ label: 'pothole', confidence: 0.48 })],
    });
    const out = overlayBoxesFor(f, { server: true, device: true });
    expect(out.map((b) => b.kind)).toEqual(['server', 'device']);
    expect(out[0]?.label).toBe('srv pothole 0.62');
    expect(out[1]?.label).toBe('dev pothole 0.48');
  });

  it('carries the geometry through unchanged', () => {
    const out = overlayBoxesFor(frame({ server_boxes: [box()] }), {
      server: true,
      device: false,
    });
    expect(out[0]).toMatchObject({ x: 0.1, y: 0.2, w: 0.3, h: 0.4 });
  });
});

function verdict(over: Partial<VlmVerdict> = {}): VlmVerdict {
  return {
    is_pothole: true,
    confidence: 0.8,
    severity: 'moderate',
    rationale: 'A clear depression in the asphalt with a broken edge.',
    model_id: 'qwen2.5vl:3b',
    ...over,
  };
}

describe('vlmSummary', () => {
  it('returns nothing when no VLM ran', () => {
    expect(vlmSummary(null)).toBeNull();
  });

  it('carries the verdict in a glyph, not only in a colour', () => {
    expect(vlmSummary(verdict())?.badge).toBe('✓ VLM: pothole');
    expect(vlmSummary(verdict({ is_pothole: false }))?.badge).toBe('✕ VLM: not a pothole');
  });

  it('always emits the blend warning', () => {
    // The whole reason parse_vlm_verdict exists: under the hybrid backend
    // server_probability is a blend, and every surface presents it as a detector score.
    expect(vlmSummary(verdict())?.blendWarning).toContain('blend');
  });

  it('returns the rationale byte-identical', () => {
    const text = '  leading and trailing space, and a \n newline  ';
    expect(vlmSummary(verdict({ rationale: text }))?.rationale).toBe(text);
  });

  it('refuses a non-string severity rather than rendering [object Object]', () => {
    // parse_detection_boxes passes verdict.get("severity") straight through with no
    // type check, so this is reachable from a misbehaving model.
    const bad = verdict({ severity: { level: 3 } as unknown as string });
    expect(vlmSummary(bad)?.severity).toBe('—');
    expect(vlmSummary(verdict({ severity: null }))?.severity).toBe('—');
  });

  it('survives a missing rationale without producing "undefined"', () => {
    const bad = verdict({ rationale: undefined as unknown as string });
    expect(vlmSummary(bad)?.rationale).toBe('');
  });
});
