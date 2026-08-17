/**
 * Bootstrap: login screen ↔ app shell.
 */

import 'maplibre-gl/dist/maplibre-gl.css';
import './tokens.css';
import './styles.css';

import { AuthError, clearSession, currentSession, isLoggedIn, login, onSessionExpired } from './auth.ts';
import { el } from './dom.ts';
import { PotholeMap, type MapView } from './map/map.ts';
import { DetailPanel } from './panel/panel.ts';
import { buildShell, readUrlState, writeUrlState } from './shell.ts';

/** No extent endpoint exists, so the initial view is configured, not discovered. */
const DEFAULT_VIEW: MapView = {
  lat: Number(import.meta.env['VITE_MAP_LAT'] ?? 43.6532),
  lon: Number(import.meta.env['VITE_MAP_LON'] ?? -79.3832),
  // Above the aggregate threshold, so the first thing an operator sees is
  // clickable individual clusters rather than un-actionable bubbles.
  zoom: Number(import.meta.env['VITE_MAP_ZOOM'] ?? 14),
};

const root = document.getElementById('app');
if (!root) throw new Error('#app not found');

let map: PotholeMap | null = null;
let panel: DetailPanel | null = null;

function renderLogin(message?: string): void {
  const emailInput = el('input', {
    class: 'input',
    type: 'email',
    id: 'email',
    name: 'email',
    autocomplete: 'username',
    required: 'required',
  });
  const passwordInput = el('input', {
    class: 'input',
    type: 'password',
    id: 'password',
    name: 'password',
    autocomplete: 'current-password',
    required: 'required',
  });
  const error = el('p', { class: 'error-text', role: 'alert', text: message ?? '' });
  const submit = el('button', { class: 'button button-primary', type: 'submit', text: 'Sign in' });

  const form = el('form', { class: 'login-form' }, [
    el('h1', { class: 'login-title', text: 'RoadWatch' }),
    el('p', { class: 'login-subtitle', text: 'Municipal road surface monitoring' }),
    el('label', { class: 'label', for: 'email', text: 'Email' }),
    emailInput,
    el('label', { class: 'label', for: 'password', text: 'Password' }),
    passwordInput,
    submit,
    error,
  ]);

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    submit.disabled = true;
    error.textContent = '';
    try {
      await login(emailInput.value, passwordInput.value);
      renderApp();
    } catch (err) {
      submit.disabled = false;
      error.textContent =
        err instanceof AuthError || err instanceof Error
          ? err.message
          : 'Sign in failed.';
    }
  });

  root!.replaceChildren(el('div', { class: 'login-screen' }, [form]));
  emailInput.focus();
}

function renderApp(): void {
  const session = currentSession();
  if (!session) return renderLogin();

  const urlState = readUrlState();
  const view = urlState.view ?? DEFAULT_VIEW;
  let assetType = urlState.asset;
  let selected: string | null = urlState.cluster;

  const { mapContainer, panelContainer } = buildShell(
    root!,
    {
      assetType,
      userEmail: session.userId,
      orgId: session.orgId,
      role: session.role || 'unknown',
    },
    {
      onAssetTypeChange: (next) => {
        assetType = next;
        map?.setAssetType(next);
        sync();
      },
      onSignOut: () => {
        clearSession();
        map?.destroy();
        map = null;
        panel = null;
        renderLogin();
      },
    },
  );

  const banner = el('div', { class: 'map-banner', hidden: 'hidden', role: 'status' });
  mapContainer.append(banner);

  function sync(): void {
    writeUrlState({ asset: assetType, view: map?.currentView() ?? view, cluster: selected });
  }

  panel = new DetailPanel(panelContainer, {
    onClose: () => {
      selected = null;
      sync();
    },
    onRepairChanged: (clusterId, repaired) => {
      // Optimistic: repaint immediately from feature state rather than waiting
      // for the 60-second tile expiry. Not combined with a tile reload, because
      // setTiles discards feature state.
      map?.markRepairedOptimistically(clusterId, repaired);
    },
    // UI hint only — the server re-reads org_member on every write.
    canRepair: () => session.role === 'staff' || session.role === 'admin',
  });

  map = new PotholeMap({
    container: mapContainer,
    initialView: view,
    assetType,
    onSelect: (clusterId) => {
      selected = clusterId;
      sync();
      void panel?.open(clusterId);
    },
    onViewChange: () => sync(),
    onError: () => {
      // Tile auth is handled inside the custom protocol; anything else is
      // usually the basemap and is not worth interrupting the operator for.
    },
  });

  // An empty view is a normal state, not a broken one — say so rather than
  // leaving a blank map that looks like a failure.
  map.onIdle(() => {
    const empty = map !== null && map.visibleFeatureCount() === 0;
    banner.hidden = !empty;
    if (empty) {
      banner.textContent = map!.isDetailZoom()
        ? 'No potholes in this view. Pan or zoom out to find collected data.'
        : 'Zoom in past level 13 to see individual potholes.';
    }
  });

  if (selected) void panel.open(selected);
  sync();
}

onSessionExpired(() => renderLogin('Your session expired. Please sign in again.'));

// MapLibre 6 requires WebGL2. Without this the failure looks like "the dashboard
// is blank", which is a miserable thing to debug over the phone with a city IT
// department.
if (!testWebgl2()) {
  root.replaceChildren(
    el('div', { class: 'login-screen' }, [
      el('div', { class: 'login-form' }, [
        el('h1', { class: 'login-title', text: 'Unsupported browser' }),
        el('p', {
          text:
            'This dashboard needs WebGL2, which this browser or graphics driver does not ' +
            'provide. This is common over remote desktop or in a virtual machine.',
        }),
      ]),
    ]),
  );
} else if (isLoggedIn()) {
  renderApp();
} else {
  renderLogin();
}

function testWebgl2(): boolean {
  try {
    return document.createElement('canvas').getContext('webgl2') !== null;
  } catch {
    return false;
  }
}
