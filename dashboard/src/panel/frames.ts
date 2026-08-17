/**
 * Frame image loading.
 *
 * Two constraints shape this:
 *
 *  - `<img src>` cannot send an Authorization header, so bytes come through
 *    fetch and become object URLs.
 *  - The server guards the image route with a Semaphore(6) that is shared across
 *    ALL users, so firing 12 requests at once would queue behind whoever else is
 *    looking at a panel. Concurrency is capped client-side well below that.
 *
 * Object URLs are revoked as soon as the image has decoded rather than tracked
 * until the panel closes: fewer moving parts, and the response is cached
 * (`private, max-age=86400, immutable`) so nothing is re-fetched.
 */

import { getFrameObjectUrl } from '../api.ts';
import type { ClusterFrameItem } from '../types.ts';

const MAX_CONCURRENT = 3;

export async function loadFrameInto(
  img: HTMLImageElement,
  frame: ClusterFrameItem,
  signal: AbortSignal,
): Promise<void> {
  // image_url is already root-relative — never prefix it, or you get
  // /api/v1/api/v1/...
  const objectUrl = await getFrameObjectUrl(frame.image_url, signal);
  if (signal.aborted) {
    URL.revokeObjectURL(objectUrl);
    return;
  }
  img.src = objectUrl;
  try {
    await img.decode();
  } catch {
    // decode() rejects if the element was removed mid-flight; nothing to do.
  } finally {
    URL.revokeObjectURL(objectUrl);
  }
}

/** Load frames with bounded concurrency, ignoring individual failures. */
export async function loadFrames(
  entries: { img: HTMLImageElement; frame: ClusterFrameItem }[],
  signal: AbortSignal,
): Promise<void> {
  let cursor = 0;
  const workers = Array.from({ length: Math.min(MAX_CONCURRENT, entries.length) }, async () => {
    while (cursor < entries.length && !signal.aborted) {
      const entry = entries[cursor++];
      if (!entry) return;
      try {
        await loadFrameInto(entry.img, entry.frame, signal);
      } catch {
        if (signal.aborted) return;
        entry.img.classList.add('frame-error');
        entry.img.alt = 'Image unavailable';
      }
    }
  });
  await Promise.all(workers);
}
