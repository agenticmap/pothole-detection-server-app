/**
 * Frame images for the review queue: a bounded object-URL cache plus a small
 * prefetch window.
 *
 * **This deliberately does NOT copy `panel/frames.ts`.** That module revokes each
 * object URL the moment the image decodes and keeps no registry, which is exactly
 * right for a thumbnail grid opened once — and wrong here, where `k` (go back one)
 * must be instant and the operator crosses hundreds of frames in a sitting. The risk
 * that policy avoids is an unbounded leak, so this cache is bounded instead: a hard
 * capacity, eviction by distance from the cursor, and revocation on eviction, on
 * queue replacement and on unmount. Do not "simplify" it back.
 *
 * **Concurrency is deliberately below the panel's.** `GET /frames/{id}/image` is
 * guarded by `Semaphore(settings.tile_max_concurrency)` = 6, shared across ALL users
 * (`app/routes/clusters.py`). A panel open is a burst; a review session is a
 * sustained consumer, so it takes a smaller share — two reviewers plus someone
 * reading a panel then sit at 5 of 6 rather than starving each other.
 *
 * The bytes themselves are cheap to re-fetch: the response is
 * `private, max-age=86400, immutable`, so a cache miss usually still misses the
 * network. What this buys is the decode and the round-trip latency.
 */

import { getFrameObjectUrl } from '../api.ts';

export const CACHE_CAPACITY = 12;
export const PREFETCH_AHEAD = 3;
/** One behind, because `k` is a correction key and corrections are usually one step. */
export const PREFETCH_BEHIND = 1;
export const MAX_CONCURRENT = 2;

interface CacheEntry {
  url: string | null;
  /** In-flight fetch, so a second request for the same frame joins rather than duplicates. */
  pending: Promise<string> | null;
  controller: AbortController | null;
  failed: boolean;
}

export class FrameImageCache {
  private readonly entries = new Map<string, CacheEntry>();
  /** Frame currently in the <img>. Never evicted — revoking a live blob URL is not portable. */
  private pinned: string | null = null;
  private inFlight = 0;
  private readonly waiting: (() => void)[] = [];

  /**
   * Resolve an object URL for a frame.
   *
   * `priority: 'now'` bypasses the concurrency gate, so pressing `k` never queues
   * behind a speculative fetch three frames ahead.
   */
  async get(clientId: string, imageUrl: string, priority: 'now' | 'prefetch'): Promise<string> {
    const existing = this.entries.get(clientId);
    if (existing?.url) return existing.url;
    if (existing?.pending) return existing.pending;

    const controller = new AbortController();
    const entry: CacheEntry = { url: null, pending: null, controller, failed: false };
    this.entries.set(clientId, entry);

    const run = async (): Promise<string> => {
      if (priority === 'prefetch') await this.acquire();
      try {
        const url = await getFrameObjectUrl(imageUrl, controller.signal);
        if (controller.signal.aborted) {
          URL.revokeObjectURL(url);
          throw new Error('aborted');
        }
        entry.url = url;
        return url;
      } catch (err) {
        entry.failed = true;
        this.entries.delete(clientId);
        throw err;
      } finally {
        entry.pending = null;
        entry.controller = null;
        if (priority === 'prefetch') this.release();
      }
    };

    entry.pending = run();
    return entry.pending;
  }

  private async acquire(): Promise<void> {
    if (this.inFlight < MAX_CONCURRENT) {
      this.inFlight++;
      return;
    }
    await new Promise<void>((resolve) => this.waiting.push(resolve));
    this.inFlight++;
  }

  private release(): void {
    this.inFlight--;
    this.waiting.shift()?.();
  }

  /** Protect the frame on screen from eviction. */
  pin(clientId: string | null): void {
    this.pinned = clientId;
  }

  /**
   * Bring the cache in line with where the operator is.
   *
   * Order matters: drop anything no longer in the queue first (those can never be
   * wanted again), then anything outside the window, then trim to capacity by
   * distance. The window carries one slot of hysteresis on each side of the prefetch
   * range so a single `k` after a `j` does not thrash.
   */
  syncWindow(ids: readonly string[], cursor: number): void {
    const index = new Map(ids.map((id, i) => [id, i]));

    for (const id of [...this.entries.keys()]) {
      if (!index.has(id)) this.evict(id);
    }

    const lo = cursor - PREFETCH_BEHIND - 1;
    const hi = cursor + PREFETCH_AHEAD + 1;
    for (const id of [...this.entries.keys()]) {
      const i = index.get(id);
      if (i === undefined) continue;
      if (i < lo || i > hi) this.evict(id);
    }

    if (this.entries.size <= CACHE_CAPACITY) return;
    const byDistance = [...this.entries.keys()]
      .filter((id) => id !== this.pinned)
      .map((id) => ({ id, d: Math.abs((index.get(id) ?? 0) - cursor) }))
      // Ties break toward the future: you are more likely to go forward than back.
      .sort((a, b) => b.d - a.d);
    for (const { id } of byDistance) {
      if (this.entries.size <= CACHE_CAPACITY) break;
      this.evict(id);
    }
  }

  /** Warm the frames around the cursor. Failures are silent — the real load reports. */
  prefetch(frames: readonly { client_id: string; image_url: string }[], cursor: number): void {
    const lo = Math.max(0, cursor - PREFETCH_BEHIND);
    const hi = Math.min(frames.length - 1, cursor + PREFETCH_AHEAD);
    for (let i = lo; i <= hi; i++) {
      const f = frames[i];
      if (!f || i === cursor) continue;
      if (this.entries.has(f.client_id)) continue;
      void this.get(f.client_id, f.image_url, 'prefetch').catch(() => {});
    }
  }

  private evict(clientId: string): void {
    if (clientId === this.pinned) return;
    const entry = this.entries.get(clientId);
    if (!entry) return;
    entry.controller?.abort();
    if (entry.url) URL.revokeObjectURL(entry.url);
    this.entries.delete(clientId);
  }

  /** Revoke everything and abort everything. Call on hide, sign-out and destroy. */
  clear(): void {
    this.pinned = null;
    for (const id of [...this.entries.keys()]) this.evict(id);
    this.entries.clear();
  }
}
