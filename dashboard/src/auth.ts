/**
 * Token lifecycle.
 *
 * Storage is **memory only**, deliberately. The refresh token is an opaque,
 * rotating, 30-day credential; persisting it to sessionStorage would mean a
 * duplicated tab (Ctrl-click copies sessionStorage) holds the same token and
 * both tabs rotate it, and a single-flight guard cannot reach across tabs.
 * Memory-only also caps what an XSS can steal at a <=30-minute access token
 * rather than a 30-day refresh token — which matters because the repair note is
 * free text that this UI echoes back. The cost is re-login after a page reload,
 * which for a single-sitting triage tool is acceptable.
 *
 * Refresh is **single-flight**. app/auth/service.py revokes the presented token
 * before issuing the new pair and there is no reuse detection, so two concurrent
 * refreshes mean one of them gets a 401. Everyone therefore awaits one shared
 * in-flight promise.
 */

export interface TokenPair {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
}

export type StaffRole = 'viewer' | 'staff' | 'admin' | string;

export interface Session {
  userId: string;
  orgId: string;
  role: StaffRole;
}

/** Refresh this long before expiry, so the map never rides a stale token. */
const REFRESH_MARGIN_MS = 5 * 60 * 1000;

let accessToken: string | null = null;
let refreshToken: string | null = null;
let expiresAt = 0;
let session: Session | null = null;
let inFlightRefresh: Promise<string> | null = null;
let onExpiredCallback: (() => void) | null = null;

/**
 * `/auth/login` and `/auth/refresh` take the ApiVersion dependency and 400
 * without this header; the tile and cluster routes deliberately do not. Sending
 * it only where it is required keeps that asymmetry visible rather than papering
 * over it.
 */
const AUTH_HEADERS = {
  'Content-Type': 'application/json',
  'Accept-Version': 'v1',
};

/** Decode a JWT payload without verifying it. UI hinting only. */
function decodeClaims(token: string): Record<string, unknown> {
  const part = token.split('.')[1];
  if (!part) return {};
  const padded = part.replace(/-/g, '+').replace(/_/g, '/');
  try {
    return JSON.parse(atob(padded + '=='.slice(0, (4 - (padded.length % 4)) % 4)));
  } catch {
    return {};
  }
}

function adopt(pair: TokenPair): void {
  accessToken = pair.access_token;
  refreshToken = pair.refresh_token;
  expiresAt = Date.now() + pair.expires_in * 1000;

  const claims = decodeClaims(pair.access_token);
  const sub = typeof claims.sub === 'string' ? claims.sub : '';
  session = {
    userId: sub.startsWith('user:') ? sub.slice(5) : sub,
    orgId: typeof claims.org === 'string' ? claims.org : '',
    // The role is a UI hint only. The server enforces it independently, and on
    // the repair route re-reads org_member rather than trusting this claim.
    role: typeof claims.role === 'string' ? claims.role : '',
  };
}

export function clearSession(): void {
  accessToken = null;
  refreshToken = null;
  expiresAt = 0;
  session = null;
  inFlightRefresh = null;
}

export function currentSession(): Session | null {
  return session;
}

export function isLoggedIn(): boolean {
  return accessToken !== null;
}

/** Called when the session cannot be recovered and the UI must return to login. */
export function onSessionExpired(cb: () => void): void {
  onExpiredCallback = cb;
}

function expire(): void {
  clearSession();
  onExpiredCallback?.();
}

export class AuthError extends Error {}

export async function login(email: string, password: string): Promise<void> {
  const res = await fetch('/api/v1/auth/login', {
    method: 'POST',
    headers: AUTH_HEADERS,
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    throw new AuthError(await readError(res));
  }
  adopt((await res.json()) as TokenPair);
}

/**
 * Return a usable access token, refreshing first if it is close to expiry.
 * Concurrent callers share one refresh.
 */
export async function getAccessToken(): Promise<string> {
  if (!accessToken || !refreshToken) throw new AuthError('Not signed in.');
  if (Date.now() < expiresAt - REFRESH_MARGIN_MS) return accessToken;
  return refreshNow();
}

/** Force a refresh. Safe to call concurrently — callers share the same promise. */
export function refreshNow(): Promise<string> {
  if (inFlightRefresh) return inFlightRefresh;

  const presented = refreshToken;
  inFlightRefresh = (async () => {
    if (!presented) throw new AuthError('Not signed in.');
    const res = await fetch('/api/v1/auth/refresh', {
      method: 'POST',
      headers: AUTH_HEADERS,
      body: JSON.stringify({ refresh_token: presented }),
    });
    if (!res.ok) {
      // Losing a refresh race is recoverable: another call may already have
      // installed a fresh pair. Only give up if our token is still the current
      // one, i.e. nothing else succeeded in the meantime.
      if (refreshToken !== presented && accessToken) return accessToken;
      expire();
      throw new AuthError('Session expired. Please sign in again.');
    }
    adopt((await res.json()) as TokenPair);
    return accessToken as string;
  })();

  return inFlightRefresh.finally(() => {
    inFlightRefresh = null;
  });
}

/**
 * Normalise the API's error envelopes into one readable string.
 *
 * There are three shapes in play: {"detail": "..."} from HTTPException,
 * {"detail": [{...}]} from Pydantic validation, and {"error":..., "detail":...}
 * from the catch-all handler. Rendering `body.detail` blindly prints
 * "[object Object]" on the validation case.
 */
export async function readError(res: Response): Promise<string> {
  let body: unknown;
  try {
    body = await res.json();
  } catch {
    return `${res.status} ${res.statusText}`;
  }
  if (body && typeof body === 'object') {
    const detail = (body as { detail?: unknown }).detail;
    if (typeof detail === 'string') return detail;
    if (Array.isArray(detail)) {
      const first = detail[0] as { msg?: string; loc?: unknown[] } | undefined;
      if (first?.msg) {
        const field = Array.isArray(first.loc) ? first.loc[first.loc.length - 1] : null;
        return field ? `${String(field)}: ${first.msg}` : first.msg;
      }
    }
    const error = (body as { error?: unknown }).error;
    if (typeof error === 'string') return error;
  }
  return `${res.status} ${res.statusText}`;
}
