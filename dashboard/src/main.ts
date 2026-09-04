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
import { getFrameDetail } from './api.ts';
import { FrameViewer } from './frameview/viewer.ts';
import { DetailPanel } from './panel/panel.ts';
import {
  buildShell,
  installLegendResponsiveness,
  setOpenCount,
  type ModuleId,
  readUrlState,
  refreshLegend,
  setLegendCounts,
  writeUrlState,
} from './shell.ts';
import { Dock } from './dock.ts';
import { ReviewModule } from './review/review.ts';
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
let review: ReviewModule | null = null;
/** Shared by the panel and the map — see where it is constructed. */
let frameViewer: FrameViewer | null = null;
/** Set by each render, so the session-expiry handler can release its resources. */
let teardownApp: (() => void) | null = null;

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
  let activeModule = urlState.module;
  // Captured rather than closed over: showModule is a hoisted function declaration,
  // which loses the `if (!session) return` narrowing above. UI hint only either way —
  // the server re-reads org_member on every write.
  const role = session.role;
  const canWrite = (): boolean => role === 'staff' || role === 'admin';

  /** Release every long-lived resource. Sign-out and session expiry both need this. */
  function teardown(): void {
    map?.destroy();
    dock?.destroy();
    review?.destroy();
    map = null;
    // The panel owns a <dialog> appended to document.body, which is OUTSIDE the root
    // the login screen replaces — so dropping the reference alone would leave the
    // viewer in the DOM across a sign-out.
    panel?.destroy();
    // Owned here, not by the panel, so it is destroyed here. It lives on
    // document.body, OUTSIDE the root the login screen replaces, so dropping the
    // reference alone would leave a <dialog> in the DOM across a sign-out.
    frameViewer?.destroy();
    frameViewer = null;
    panel = null;
    dock = null;
    review = null;
  }
  teardownApp = teardown;

  const { mapContainer, panelContainer, moduleContainer, setActiveModule } = buildShell(
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
        teardown();
        renderLogin();
      },
      onThemeChange: (theme) => {
        // The chrome re-themes itself from the token block; the map and the
        // legend do not, because both bake CSS variables into literal values.
        map?.applyTheme(theme);
        refreshLegend();
      },
      onModuleChange: (next) => {
        void showModule(next);
      },
    },
  );

  const banner = el('div', { class: 'map-banner', hidden: 'hidden', role: 'status' });
  mapContainer.append(banner);

  function sync(): void {
    // writeUrlState rebuilds the query from scratch, so the review slice has to be
    // threaded through here or the next map pan silently drops every review key.
    writeUrlState({
      asset: assetType,
      view: map?.currentView() ?? view,
      cluster: selected,
      module: activeModule,
      review: activeModule === 'review' ? (review?.urlState() ?? urlState.review) : null,
    });
  }

  /**
   * Swap modules by visibility, never by teardown.
   *
   * The map keeps its WebGL context and its loaded tiles, so returning to it is
   * instant. Two ordering rules: never resize while hidden (a display:none container
   * reports clientWidth 0 and MapLibre would size its canvas to 0x0), and resize on
   * the way back inside a rAF so layout has settled first.
   */
  async function showModule(next: ModuleId): Promise<void> {
    activeModule = next;
    setActiveModule(next);
    const onMap = next === 'map';

    mapContainer.hidden = !onMap;
    panelContainer.hidden = !onMap;
    moduleContainer.hidden = onMap;

    if (onMap) {
      review?.hide();
      requestAnimationFrame(() => map?.resize());
    } else {
      // Constructed lazily: an operator who never opens review never pays for it.
      if (!review) {
        review = new ReviewModule(moduleContainer, {
          onStateChange: () => sync(),
          canWrite,
        });
        review.applyUrlState(urlState.review);
      }
      await review.show();
    }
    sync();
  }


  /**
   * Open a frame at full size from the map.
   *
   * Needs a round trip: the frames tile carries `server_box_count` but not the boxes,
   * because frame-relative geometry is meaningless in map space. Serves unpaired
   * frames too, which is why the endpoint returns FrameDetail rather than the
   * cluster-shaped ClusterFrameItem.
   */
  async function openFrameFullSize(clientId: string): Promise<void> {
    try {
      const frame = await getFrameDetail(clientId);
      frameViewer?.open({ frames: [frame], index: 0, trigger: null });
    } catch {
      // The popup is still open with the facts it already had; a failed enlargement
      // is not worth an alert over.
    }
  }

  // ONE viewer for the whole app. Two would mean two <dialog>s on document.body,
  // each able to be open at once and each inerting the other's document.
  frameViewer = new FrameViewer();

  panel = new DetailPanel(panelContainer, {
    onClose: () => {
      selected = null;
      map?.setSelected(null);
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

  dock = new Dock(
    mapContainer,
    // The mockup's heading is the municipality's display name. The token and the
    // login response carry only org_id, so this shows that until org.name is
    // threaded through /auth/login — real data either way, just terser.
    { scope: session.orgId },
    {
      onFilterChange: (filter) => {
        map?.setClusterFilter(filter.tiers, filter.minDevices, {
          selected: filter.sources,
          all: dock!.allSourcesSelected(),
        });
        map?.setObservationFilter(filter.observationClasses);
        setOpenCount(dock!.shownCount(), lastOpenTotal);
      },
      onObservationsToggle: (visible) => map?.setObservationsVisible(visible),
      onFramesToggle: (visible) => map?.setFramesVisible(visible),
    },
  );

  map = new PotholeMap({
    container: mapContainer,
    initialView: view,
    assetType,
    onSelect: (clusterId) => {
      selected = clusterId;
      map?.setSelected(clusterId);
      sync();
      void panel?.open(clusterId);
    },
    onViewChange: () => {
      sync();
      scheduleStats();
    },
    onHover: (feature) => dock?.setReadout(feature),
    onOpenFrame: (clientId) => void openFrameFullSize(clientId),
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
  let lastOpenTotal = 0;

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
      lastOpenTotal = stats.open;
      setLegendCounts(stats.tier_counts, stats.unrated, stats.repaired);
      setOpenCount(dock.shownCount(), stats.open);
    } catch (err) {
      if (controller.signal.aborted) return;
      // A failed count must not leave a stale one on screen looking current.
      dock.setUnavailable();
      setLegendCounts(null, null, null);
      setOpenCount(null, null);
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

  if (selected) {
    map.setSelected(selected);
    void panel.open(selected);
  }

  // Applied last, so the map is fully constructed before it may be hidden. Also the
  // only thing that lights the rail: setActiveModule is the single writer of
  // is-active, so the highlight follows state — including state arriving from a
  // shared #/m=review link — rather than only from a click.
  void showModule(activeModule);
}

onSessionExpired(() => {
  // Previously this rendered the login screen over a live map, leaking its WebGL
  // context and, now, the review module's object URLs and abort controllers.
  teardownApp?.();
  renderLogin('Your session expired. Please sign in again.');
});

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
