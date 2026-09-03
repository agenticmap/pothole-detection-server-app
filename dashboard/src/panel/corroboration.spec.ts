import { describe, expect, it } from 'vitest';

import { formatSpan, spanNoteText } from './corroboration.ts';

describe('formatSpan', () => {
  it('keeps sub-minute spans in seconds, which is the case that matters', () => {
    expect(formatSpan(12)).toBe('12 s');
    expect(formatSpan(59.4)).toBe('59 s');
  });

  it('steps up through minutes, hours and days', () => {
    expect(formatSpan(300)).toBe('5 min');
    expect(formatSpan(7200)).toBe('2 h');
    expect(formatSpan(259200)).toBe('3.0 days');
  });

  it('drops the decimal only past ten days', () => {
    expect(formatSpan(86400 * 9.5)).toBe('9.5 days');
    expect(formatSpan(86400 * 30)).toBe('30 days');
  });

  it('renders zero as a span rather than an em dash', () => {
    // Zero is a real measurement -- every member on the same instant -- and is the
    // strongest possible evidence of a single drive-past. It must not read as "no data".
    expect(formatSpan(0)).toBe('0 s');
  });

  it('refuses nonsense instead of rendering NaN', () => {
    expect(formatSpan(Number.NaN)).toBe('—');
    expect(formatSpan(-1)).toBe('—');
  });
});

describe('spanNoteText', () => {
  it('says nothing when the cluster is actually corroborated', () => {
    expect(spanNoteText(2, 259200)).toBeNull();
    expect(spanNoteText(7, 12)).toBeNull();
  });

  it('calls out the single drive-past, which is what the whole corpus looks like', () => {
    expect(spanNoteText(1, 12)).toBe(
      'All observations within 12 s — one drive-past, not repeat corroboration.',
    );
  });

  it('distinguishes one long pass from one brief one', () => {
    expect(spanNoteText(1, 3600)).toBe('One pass spanning 1 h — no repeat coverage yet.');
  });

  it('handles a cluster with no span at all', () => {
    expect(spanNoteText(1, null)).toBe('One pass — nothing has corroborated this defect yet.');
  });

  it('treats a zero pass count as uncorroborated, not as corroborated', () => {
    // 015 defaults the column to 0, so a cluster the job has not recomputed reads 0.
    // Falling through to "no note" there would be the original bug wearing a new hat.
    expect(spanNoteText(0, 12)).not.toBeNull();
  });

  it('does not lose the warning at exactly one minute', () => {
    expect(spanNoteText(1, 60)).toBe('One pass spanning 1 min — no repeat coverage yet.');
    expect(spanNoteText(1, 59)).toContain('one drive-past');
  });
});
