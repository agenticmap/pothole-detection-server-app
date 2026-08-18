/**
 * Authenticated API client.
 *
 * Every call goes through `request`, which attaches the bearer, retries exactly
 * once on a 401 after a refresh, and normalises the API's three error envelopes
 * into a readable message.
 */

import { AuthError, getAccessToken, readError, refreshNow } from './auth.ts';
import type { ClusterDetailResponse, RepairResponse } from './types.ts';

export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number,
  ) {
    super(message);
  }
}

export async function request(
  path: string,
  init: RequestInit = {},
  retried = false,
): Promise<Response> {
  const token = await getAccessToken();
  const headers = new Headers(init.headers);
  headers.set('Authorization', `Bearer ${token}`);

  const res = await fetch(path, { ...init, headers });

  // One retry only. A second 401 after a successful refresh means the token is
  // genuinely not accepted, and looping would just hammer the auth endpoint.
  if (res.status === 401 && !retried) {
    await refreshNow();
    return request(path, init, true);
  }
  if (!res.ok) {
    throw new ApiError(await readError(res), res.status);
  }
  return res;
}

export async function getCluster(
  clusterId: string,
  signal?: AbortSignal,
): Promise<ClusterDetailResponse> {
  const res = await request(`/api/v1/clusters/${encodeURIComponent(clusterId)}`, { signal });
  return (await res.json()) as ClusterDetailResponse;
}

export async function setRepaired(
  clusterId: string,
  repaired: boolean,
  note: string | null,
): Promise<RepairResponse> {
  const res = await request(`/api/v1/clusters/${encodeURIComponent(clusterId)}/repair`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ repaired, note }),
  });
  return (await res.json()) as RepairResponse;
}

/**
 * Fetch a frame image as an object URL.
 *
 * <img src> cannot send an Authorization header, so the bytes have to come
 * through fetch and become a blob URL. `image_url` from the API is already
 * root-relative — passed through verbatim.
 */
export async function getFrameObjectUrl(imageUrl: string, signal?: AbortSignal): Promise<string> {
  const res = await request(imageUrl, { signal });
  return URL.createObjectURL(await res.blob());
}

export { AuthError };
