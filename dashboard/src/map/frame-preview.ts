/**
 * The decision logic behind the map's frame preview. Pure — no DOM, no fetch.
 *
 * Extracted for the same reason `frame-facts.ts` was: the suite is node-environment
 * with no jsdom, so anything that touches an element cannot be tested at all. What is
 * worth testing here is not the `<img>` — it is the race.
 *
 * THE RACE. One popup is reused for every marker. Click A, then click B while A's
 * image is still in flight, and A's blob arrives into B's popup. The frame viewer
 * already solves this with a captured id re-checked before applying
 * (`frameview/viewer.ts`); this is the same guard as a state machine so it can be
 * asserted rather than eyeballed.
 */

/** The authenticated API path for a frame's JPEG. */
export function framePreviewUrl(clientId: unknown): string | null {
  if (typeof clientId !== 'string') return null;
  const id = clientId.trim();
  if (id === '') return null;
  // Encoded even though ids are UUIDs today: this value comes off a vector tile, which
  // is server-built but not a contract anything here enforces, and it is being pasted
  // into a URL path.
  return `/api/v1/frames/${encodeURIComponent(id)}/image`;
}

export type PreviewPhase = 'idle' | 'loading' | 'loaded' | 'failed';

export interface PreviewState {
  phase: PreviewPhase;
  /** The frame this state is about. Everything else is only valid for this id. */
  clientId: string | null;
}

export const IDLE: PreviewState = { phase: 'idle', clientId: null };

export type PreviewEvent =
  | { type: 'open'; clientId: string }
  | { type: 'loaded'; clientId: string }
  | { type: 'failed'; clientId: string }
  | { type: 'closed' };

/**
 * Advance the preview state.
 *
 * The whole point is the two guards on late arrivals:
 *
 *   - a `loaded` for a frame we are no longer showing is DROPPED, so clicking B while
 *     A loads cannot paint A's photograph into B's popup;
 *   - a `loaded` after `closed` is dropped too, because the popup that asked is gone
 *     and the object URL it would have used has already been revoked.
 *
 * Both are silent by design: there is nothing to tell the operator, and the alternative
 * is showing them the wrong road.
 */
export function reducePreview(state: PreviewState, event: PreviewEvent): PreviewState {
  switch (event.type) {
    case 'open':
      return { phase: 'loading', clientId: event.clientId };
    case 'loaded':
    case 'failed':
      // Stale: either a different frame is showing now, or none is.
      if (state.clientId === null || state.clientId !== event.clientId) return state;
      return { phase: event.type === 'loaded' ? 'loaded' : 'failed', clientId: state.clientId };
    case 'closed':
      return IDLE;
  }
}
