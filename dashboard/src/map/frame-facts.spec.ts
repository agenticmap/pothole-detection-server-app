import { describe, expect, it } from 'vitest';

import { frameFacts, frameStatus } from './frame-facts.ts';

const valueOf = (props: Record<string, unknown>, term: string): string | undefined =>
  frameFacts(props).find(([t]) => t === term)?.[1];

describe('frameStatus', () => {
  it('reports an unscored frame as unscored', () => {
    const s = frameStatus({});
    expect(s.severe).toBe(true);
    expect(s.text).toContain('Not yet scored');
  });

  it('checks unscored BEFORE unpaired', () => {
    // A frame that is neither scored nor paired must report the cause, not the
    // consequence. Reversing these two branches would tell the operator no sensor
    // event matched, when in fact the detector never ran.
    const s = frameStatus({ detected: false, paired: false });
    expect(s.text).toContain('Not yet scored');
    expect(s.text).not.toContain('unpaired');
  });

  it('reports a scored frame that reached no cluster', () => {
    const s = frameStatus({ detected: true, paired: false });
    expect(s.severe).toBe(true);
    expect(s.text).toContain('unpaired');
  });

  it('is not severe once the frame fused', () => {
    expect(frameStatus({ detected: true, paired: true })).toEqual({
      text: 'Paired with a sensor event and fused.',
      severe: false,
    });
  });

  it('treats a missing flag as false rather than truthy', () => {
    // Tile properties are `unknown`; `detected: 0` or a missing key must not read as
    // scored. The comparison is `!== true` for exactly this reason.
    expect(frameStatus({ detected: 0, paired: 1 }).text).toContain('Not yet scored');
  });
});

describe('frameFacts', () => {
  it('renders a zero server score as a score, not as "not scored"', () => {
    // 2,183 of the originally scored frames are exactly 0.0. That means the detector
    // ran and found nothing -- a result. Only a NULL means nothing looked.
    expect(valueOf({ server_probability: 0 }, 'Server p')).toBe('0.000');
  });

  it('says "not scored" only when the score is genuinely absent', () => {
    expect(valueOf({}, 'Server p')).toBe('not scored');
  });

  it('always includes the on-device row, even when it is empty', () => {
    // It used to be omitted when null, so the popup changed shape between frames and
    // two frames with different provenance could render identically.
    expect(valueOf({}, 'On-device p')).toBe('—');
    expect(valueOf({ device_probability: 0.48 }, 'On-device p')).toBe('0.480');
  });

  it('labels the count as server boxes, since that is what it counts', () => {
    expect(valueOf({ server_box_count: 3 }, 'Server boxes')).toBe('3');
    expect(valueOf({ server_box_count: 0 }, 'Server boxes')).toBe('0');
    // The old label was "Boxes found", which claimed to cover the device set too.
    expect(valueOf({ server_box_count: 3 }, 'Boxes found')).toBeUndefined();
  });

  it('marks the primary pairing', () => {
    expect(valueOf({ fused_confidence: 0.8, is_primary: true }, 'Fused confidence')).toBe(
      '0.800 (primary)',
    );
    expect(valueOf({ fused_confidence: 0.8 }, 'Fused confidence')).toBe('0.800');
  });

  it('appends a tile property it has not been taught about', () => {
    // The mechanism that stops a new column being silently swallowed: the KNOWN_PROPS
    // set and the row list have to be updated together, and this fails if only one is.
    expect(valueOf({ vlm_verified: 'yes' }, 'vlm_verified')).toBe('yes');
  });

  it('does not append a property it already rendered', () => {
    const terms = frameFacts({ server_probability: 0.5, device_probability: 0.2 }).map(
      ([t]) => t,
    );
    expect(terms).not.toContain('server_probability');
    expect(terms).not.toContain('device_probability');
  });

  it('refuses a nonsense timestamp instead of rendering "Invalid Date"', () => {
    expect(valueOf({ ts_epoch: Number.NaN }, 'Captured')).toBeUndefined();
    expect(valueOf({ ts_epoch: 1_756_000_000 }, 'Captured')).toMatch(
      /^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}Z$/,
    );
  });
});
