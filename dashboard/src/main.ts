/**
 * Bootstrap: login screen ↔ app shell.
 */

import 'maplibre-gl/dist/maplibre-gl.css';
import './tokens.css';
import './styles.css';
// The Organic skin is an override layer, not a replacement — it targets the same
// class names styles.css defines and wins only by loading last.
import './organic-shell.css';

import { AuthError, clearSession, currentSession, isLoggedIn, login, onSessionExpired } from './auth.ts';
import { el } from './dom.ts';
import { PotholeMap, type MapView } from './map/map.ts';
import { DetailPanel } from './panel/panel.ts';
import {
  buildShell,
  installLegendResponsiveness,
  readUrlState,
  refreshLegend,
  setLegendCounts,
  writeUrlState,
} from './shell.ts';
import { Dock } from './dock.ts';
import { getStats } from './stats.ts';
import { initTheme } from './theme.ts';

// Applied before the first render so there is no light flash on a dark-theme load.
initTheme();

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
let dock: Dock | null = null;

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
      userEmail: session.email ?? session.userId,
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
        dock?.destroy();
        map = null;
        panel = null;
        dock = null;
        renderLogin();
      },
      onThemeChange: (theme) => {
        // The chrome re-themes itself from the token block; the map and the
        // legend do not, because both bake CSS variables into literal values.
        map?.applyTheme(theme);
        refreshLegend();
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

  dock = new Dock(mapContainer, {
    onFilterChange: (filter) => {
      map?.setClusterFilter(filter.tiers, filter.minDevices);
    },
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
    onViewChange: () => {
      sync();
      scheduleStats();
    },
    onHover: (feature) => dock?.setReadout(feature),
    onError: () => {
      // Tile auth is handled by installTileAuthRecovery; anything else is usually
      // the basemap and is not worth interrupting the operator for.
    },
  });

  // ── Viewport statistics ─────────────────────────────────────────────────────
  // Debounced, and every request carries an AbortController so a fast pan cannot
  // land an older response on top of a newer one — the same discipline panel.ts
  // uses for cluster detail.
  let statsTimer: ReturnType<typeof setTimeout> | null = null;
  let statsRequest: AbortController | null = null;

  function scheduleStats(): void {
    if (statsTimer !== null) clearTimeout(statsTimer);
    statsTimer = setTimeout(() => void fetchStats(), 300);
  }

  async function fetchStats(): Promise<void> {
    if (!map || !dock) return;
    statsRequest?.abort();
    const controller = new AbortController();
    statsRequest = controller;
    try {
      const stats = await getStats(map.bounds(), assetType, controller.signal);
      dock.update(stats);
      setLegendCounts(stats.tier_counts, stats.unrated, stats.repaired);
    } catch (err) {
      if (controller.signal.aborted) return;
      // A failed count must not leave a stale one on screen looking current.
      dock.setUnavailable();
      setLegendCounts(null, null, null);
      // eslint-disable-next-line no-console
      console.warn('[stats]', err);
    }
  }

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
    // The chips filter attributes that only individual features carry.
    dock?.setFiltersApply(map?.isDetailZoom() ?? false);
  });

  installLegendResponsiveness(mapContainer, () => map?.resize());

  void fetchStats();

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
