import { describe, expect, it } from 'vitest';

import { framePreviewUrl, IDLE, type PreviewState, reducePreview } from './frame-preview.ts';

describe('framePreviewUrl', () => {
  it('builds the authenticated path, not a storage path', () => {
    // The stored path is "{device_id}/{client_id}.jpg" and is never exposed; the
    // endpoint is keyed on client_id alone.
    expect(framePreviewUrl('abc-123')).toBe('/api/v1/frames/abc-123/image');
  });

  it('encodes the id', () => {
    // The value comes off a vector tile and is being pasted into a URL path.
    expect(framePreviewUrl('a/../b')).toBe('/api/v1/frames/a%2F..%2Fb/image');
  });

  it('refuses anything that is not a usable id', () => {
    // Tile properties are `unknown`; a missing client_id must produce no request at
    // all rather than a request to /api/v1/frames//image.
    for (const bad of [undefined, null, '', '   ', 42, {}]) {
      expect(framePreviewUrl(bad)).toBeNull();
    }
  });
});

describe('reducePreview', () => {
  const loadingA: PreviewState = { phase: 'loading', clientId: 'A' };

  it('starts loading on open', () => {
    expect(reducePreview(IDLE, { type: 'open', clientId: 'A' })).toEqual(loadingA);
  });

  it('shows the image when its own load lands', () => {
    expect(reducePreview(loadingA, { type: 'loaded', clientId: 'A' })).toEqual({
      phase: 'loaded',
      clientId: 'A',
    });
  });

  it('DROPS a load that lands after the operator clicked another marker', () => {
    // THE RACE THIS FILE EXISTS FOR. One popup is reused for every marker, so
    // without this A's photograph appears in B's popup — showing the operator a
    // different piece of road than the one they clicked.
    const loadingB = reducePreview(loadingA, { type: 'open', clientId: 'B' });
    const late = reducePreview(loadingB, { type: 'loaded', clientId: 'A' });
    expect(late).toEqual(loadingB);
    expect(late.phase).toBe('loading');
  });

  it('drops a load that lands after the popup closed', () => {
    // The object URL was revoked on close, so applying this would set src to a
    // revoked blob — a broken image with no explanation.
    const closed = reducePreview(loadingA, { type: 'closed' });
    expect(reducePreview(closed, { type: 'loaded', clientId: 'A' })).toEqual(IDLE);
  });

  it('drops a stale failure too, so B does not inherit A error', () => {
    const loadingB = reducePreview(loadingA, { type: 'open', clientId: 'B' });
    expect(reducePreview(loadingB, { type: 'failed', clientId: 'A' })).toEqual(loadingB);
  });

  it('reports a genuine failure', () => {
    expect(reducePreview(loadingA, { type: 'failed', clientId: 'A' })).toEqual({
      phase: 'failed',
      clientId: 'A',
    });
  });

  it('re-opening the same frame restarts the load', () => {
    // Closing and re-clicking one marker must fetch again: the object URL was
    // revoked, so the previous `loaded` state is not reusable.
    const closed = reducePreview({ phase: 'loaded', clientId: 'A' }, { type: 'closed' });
    expect(reducePreview(closed, { type: 'open', clientId: 'A' })).toEqual(loadingA);
  });
});
