/**
 * Attaching the bearer token to tile requests.
 *
 * This uses `transformRequest`, after `addProtocol` was tried and empirically
 * rejected: MapLibre 6 loads vector tiles in a Web Worker, and a protocol
 * registered with `addProtocol` on the main thread is never consulted for them
 * (registering in the worker via `importScriptInWorkers` would work, but the
 * access token lives on the main thread and would have to be shipped into the
 * worker on every refresh). `transformRequest`, by contrast, runs on the main
 * thread while the tile URL is being built and its headers are handed to the
 * worker with the request — so it works, and the token never leaves the main
 * thread.
 *
 * Two rules matter here:
 *
 *  1. Return a plain object SYNCHRONOUSLY for anything that is not our API. The
 *     hook applies to every resource type, including the raster basemap, and an
 *     `async` function returns a Promise for every call — putting the basemap
 *     onto the awaited code path, which had two abort-related bugs fixed as
 *     recently as MapLibre 6.1. Only API requests take the async branch.
 *  2. Match on the `/api/v1/` PATHNAME, not on the origin. In dev the API origin
 *     *is* the dashboard origin (Vite proxy), so an origin test would attach the
 *     bearer to index.html and every static asset.
 *
 * `transformRequest` cannot see responses, so a 401 leaves the tile in
 * `state = 'errored'` and it never retries itself. Recovery is handled by
 * `installTileAuthRecovery` below.
 */

import type { AJAXError, Map as MapLibreMap, RequestParameters, ResourceType } from 'maplibre-gl';
import { getAccessToken, refreshNow } from '../auth.ts';

const API_PREFIX = '/api/v1/';

/** Access token as of the last refresh, readable synchronously. */
let cachedToken: string | null = null;

export function primeTokenCache(token: string): void {
  cachedToken = token;
}

function isApiRequest(url: string): boolean {
  try {
    return new URL(url, location.href).pathname.startsWith(API_PREFIX);
  } catch {
    return false;
  }
}

export function transformRequest(
  url: string,
  _resourceType?: ResourceType,
): RequestParameters | Promise<RequestParameters> {
  if (!isApiRequest(url)) return { url };

  // Fast path: a token we already hold. Keeps the map off the awaited branch.
  if (cachedToken) {
    return { url, headers: { Authorization: `Bearer ${cachedToken}` } };
  }
  return getAccessToken().then((token) => {
    cachedToken = token;
    return { url, headers: { Authorization: `Bearer ${token}` } };
  });
}

/**
 * Recover from an expired token.
 *
 * An errored tile stays errored, so refreshing alone would leave a blank map.
 * On a 401 we refresh once, re-prime the cache, and force the source to refetch.
 */
export function installTileAuthRecovery(map: MapLibreMap, reloadTiles: () => void): void {
  let recovering = false;

  map.on('error', (event) => {
    const status = (event.error as AJAXError | undefined)?.status;
    if (status !== 401 || recovering) return;

    recovering = true;
    void refreshNow()
      .then((token) => {
        cachedToken = token;
        reloadTiles();
      })
      .catch(() => {
        // Session is genuinely gone; auth.ts has already notified the app via
        // its onSessionExpired callback, which returns the UI to the login screen.
      })
      .finally(() => {
        recovering = false;
      });
  });
}

/** Keep the synchronous cache in step with a proactively refreshed token. */
export function refreshTokenCache(): Promise<void> {
  return getAccessToken().then((token) => {
    cachedToken = token;
  });
}

export function clearTokenCache(): void {
  cachedToken = null;
}
