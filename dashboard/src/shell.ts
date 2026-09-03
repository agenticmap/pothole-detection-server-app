/**
 * Application shell: top bar, module rail, legend, and URL state.
 *
 * The rail carries modules that do not exist yet (Inventory, Work orders,
 * Reports, Admin), rendered disabled. That is deliberate: the competitor set
 * converges on Collect → Assess → Prioritise → Act → Track, and this product
 * currently stops at Prioritise. Having the slots now means adding a work-order
 * module later is additive rather than a re-layout.
 *
 * Asset type is a first-class selector rather than a hardcoded "Potholes"
 * because the tile and detail endpoints already take `asset_type` — Phase 5's
 * multi-asset expansion should be a registry entry, not a restructure.
 */

import { el } from './dom.ts';
import { SEVERITY_TIERS, UNRATED_LABEL, severityColors, unknownColor } from './severity.ts';
import { cssVar } from './tokens.ts';
import { currentTheme, toggleTheme, type Theme } from './theme.ts';
import type { MapView } from './map/map.ts';

export interface AssetType {
  id: string;
  label: string;
  enabled: boolean;
}

/** Only potholes exist today; the shape is what matters. */
export const ASSET_TYPES: AssetType[] = [
  { id: 'pothole', label: 'Potholes', enabled: true },
  { id: 'sign', label: 'Signs', enabled: false },
  { id: 'streetlight', label: 'Streetlights', enabled: false },
  { id: 'crosswalk', label: 'Crosswalks', enabled: false },
];

/** Modules that exist. Everything else in the rail is a deliberately disabled slot. */
export type ModuleId = 'map' | 'review';

const MODULES: { id: string; label: string; glyph: string; enabled: boolean }[] = [
  { id: 'map', label: 'Map', glyph: 'M', enabled: true },
  // 'F', not 'R': the brand mark is already an R disc and Reports below wants R too.
  { id: 'review', label: 'Frame review', glyph: 'F', enabled: true },
  { id: 'inventory', label: 'Inventory', glyph: 'I', enabled: false },
  { id: 'work-orders', label: 'Work orders', glyph: 'W', enabled: false },
  { id: 'reports', label: 'Reports', glyph: 'R', enabled: false },
  { id: 'admin', label: 'Admin', glyph: 'A', enabled: false },
];

/** The review module's slice of the hash, so a colleague can be sent one frame. */
export interface ReviewUrlState {
  mode: 'verdict' | 'box';
  order: 'score' | 'blind';
  review: boolean;
  minScore: number | null;
  maxScore: number | null;
  seed: number | null;
  frame: string | null;
}

export interface UrlState {
  asset: string;
  view: MapView | null;
  cluster: string | null;
  module: ModuleId;
  review: ReviewUrlState | null;
}

/**
 * State lives in the hash so an operator can send a colleague a link to the
 * exact defect, and the browser back button behaves. The hash needs no server
 * support, which is why there is no SPA catch-all route to get wrong.
 */
export function readUrlState(): UrlState {
  const params = new URLSearchParams(location.hash.replace(/^#\/?/, ''));

  // Parse via a helper rather than Number() directly: Number(null) is 0, not NaN,
  // so an absent parameter would otherwise read as a perfectly valid coordinate
  // and drop a first-time visitor at Null Island (0, 0) at zoom 0.
  const num = (key: string): number | null => {
    const raw = params.get(key);
    if (raw === null || raw.trim() === '') return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };

  const lat = num('lat');
  const lon = num('lon');
  const zoom = num('z');

  // "band=0.30:" and "band=:0.40" are both legal — a band open at one end is the
  // normal case. Split on a colon rather than a dash so a negative never has to be
  // disambiguated, and parse each half through the same absent-vs-zero guard,
  // because min_score=0 is meaningful.
  const band = (params.get('band') ?? '').split(':');
  const half = (raw: string | undefined): number | null => {
    if (raw === undefined || raw.trim() === '') return null;
    const value = Number(raw);
    return Number.isFinite(value) ? value : null;
  };

  const module: ModuleId = params.get('m') === 'review' ? 'review' : 'map';
  return {
    asset: params.get('asset') ?? 'pothole',
    view: lat !== null && lon !== null && zoom !== null ? { lat, lon, zoom } : null,
    cluster: params.get('cluster'),
    module,
    review:
      module === 'review'
        ? {
            mode: params.get('qmode') === 'box' ? 'box' : 'verdict',
            // Carried explicitly: without it a shared blind link would degrade to
            // score ordering, which is a different anti-anchoring posture than the
            // sender chose.
            order: params.get('qorder') === 'blind' ? 'blind' : 'score',
            review: params.get('qreview') === '1',
            minScore: half(band[0]),
            maxScore: half(band[1]),
            seed: num('seed'),
            frame: params.get('frame'),
          }
        : null,
  };
}

export function writeUrlState(state: UrlState): void {
  const params = new URLSearchParams();
  params.set('asset', state.asset);
  // Map keys are written in EVERY module, so switching to review and back returns
  // the operator to the viewport they left.
  if (state.view) {
    params.set('z', state.view.zoom.toFixed(2));
    params.set('lat', state.view.lat.toFixed(6));
    params.set('lon', state.view.lon.toFixed(6));
  }
  if (state.cluster) params.set('cluster', state.cluster);
  // Omitted for 'map', so every link written before this module existed still
  // round-trips byte-identically.
  if (state.module !== 'map') params.set('m', state.module);
  const r = state.review;
  if (r) {
    params.set('qmode', r.mode);
    params.set('qorder', r.order);
    if (r.review) params.set('qreview', '1');
    if (r.minScore !== null || r.maxScore !== null) {
      params.set('band', `${r.minScore ?? ''}:${r.maxScore ?? ''}`);
    }
    if (r.order === 'blind' && r.seed !== null) params.set('seed', String(r.seed));
    if (r.frame) params.set('frame', r.frame);
  }
  const next = `#/${params.toString()}`;
  if (next !== location.hash) history.replaceState(null, '', next);
}

/** The top bar's open-defect tag. Module scope so main.ts can refresh it. */
let openCountTag: HTMLElement | null = null;

/**
 * Update the open-defect tag.
 *
 * `shown` is what survives the dock's filters, `total` is the whole viewport.
 * When they differ the tag says so — a bare "66 open defects" while a filter is
 * quietly hiding 29 others would misrepresent the backlog.
 */
export function setOpenCount(shown: number | null, total: number | null): void {
  if (!openCountTag) return;
  if (shown === null || total === null) {
    openCountTag.textContent = '—';
    return;
  }
  openCountTag.textContent =
    shown === total ? `${total} open defects` : `${shown} of ${total} open defects`;
}

export interface ShellCallbacks {
  onAssetTypeChange: (assetType: string) => void;
  onSignOut: () => void;
  /** Fired after the theme flips, so the map can repaint its layers. */
  onThemeChange: (theme: Theme) => void;
  onModuleChange: (module: ModuleId) => void;
}

/**
 * Theme toggle. Icon-only, so it carries an aria-label and a title that both
 * describe the ACTION rather than the current state — "Dark mode" when clicking
 * would give you dark.
 */
function buildThemeToggle(onChange: (theme: Theme) => void): HTMLElement {
  const button = el('button', { class: 'icon-button theme-toggle', type: 'button' });

  const render = (theme: Theme) => {
    const next = theme === 'dark' ? 'Light mode' : 'Dark mode';
    button.textContent = theme === 'dark' ? '☀' : '☾';
    button.setAttribute('aria-label', next);
    button.setAttribute('title', next);
  };

  render(currentTheme());
  button.addEventListener('click', () => {
    const theme = toggleTheme();
    render(theme);
    onChange(theme);
  });
  return button;
}

export function buildShell(
  root: HTMLElement,
  opts: { assetType: string; userEmail: string; orgId: string; role: string },
  callbacks: ShellCallbacks,
): {
  mapContainer: HTMLElement;
  panelContainer: HTMLElement;
  moduleContainer: HTMLElement;
  setActiveModule: (id: ModuleId) => void;
} {
  const rail = el('nav', { class: 'rail', 'aria-label': 'Modules' });
  const railButtons = new Map<string, HTMLButtonElement>();
  for (const module of MODULES) {
    const button = el('button', {
      class: module.enabled ? 'rail-item' : 'rail-item is-disabled',
      type: 'button',
      disabled: !module.enabled,
      title: module.enabled ? module.label : `${module.label} — not yet available`,
      'aria-label': module.label,
    });
    button.append(
      el('span', { class: 'rail-glyph', text: module.glyph, 'aria-hidden': 'true' }),
      el('span', { class: 'rail-label', text: module.label }),
    );
    if (module.enabled) {
      railButtons.set(module.id, button);
      button.addEventListener('click', () => {
        // The rail asks; it does not decide. setActiveModule is the only writer of
        // is-active, so the highlight also follows state arriving from the hash on
        // load rather than only from a click.
        callbacks.onModuleChange(module.id as ModuleId);
        // A document-level keydown handler plus a focused button means Space and
        // Enter would fire the click AND the shortcut.
        button.blur();
      });
    }
    rail.append(button);
  }

  /** Single writer of the rail's active state, so it cannot disagree with the app. */
  const setActiveModule = (id: ModuleId): void => {
    for (const [moduleId, button] of railButtons) {
      const active = moduleId === id;
      button.classList.toggle('is-active', active);
      if (active) button.setAttribute('aria-current', 'page');
      else button.removeAttribute('aria-current');
    }
  };

  const assetSelect = el('select', { class: 'asset-select', 'aria-label': 'Asset type' });
  for (const asset of ASSET_TYPES) {
    assetSelect.append(
      el('option', {
        value: asset.id,
        disabled: !asset.enabled,
        selected: asset.id === opts.assetType,
        text: asset.enabled ? asset.label : `${asset.label} (soon)`,
      }),
    );
  }
  assetSelect.addEventListener('change', () => callbacks.onAssetTypeChange(assetSelect.value));

  // Mockup line 77: a sage tag after the asset select. Fully backed by the stats
  // endpoint, and honest when filtered — "66 of 95" rather than a bare 66.
  openCountTag = el('span', { class: 'tag tag-accent-2 open-count', text: '—' });

  const signOut = el('button', { class: 'link-button', type: 'button', text: 'Sign out' });
  signOut.addEventListener('click', callbacks.onSignOut);

  const topbar = el('header', { class: 'topbar' }, [
    el('div', { class: 'brand' }, [
      // The mark is a letter on a terracotta disc; organic-shell.css sizes it and
      // gives it the display face.
      el('span', { class: 'brand-mark', 'aria-hidden': 'true', text: 'R' }),
      el('div', { class: 'brand-lockup' }, [
        el('span', { class: 'brand-name', text: 'RoadWatch' }),
        el('span', { class: 'brand-sub', text: 'Operator console' }),
      ]),
    ]),
    assetSelect,
    openCountTag,
    el('div', { class: 'topbar-spacer' }),
    el('div', { class: 'user-block' }, [
      el('span', { class: 'user-email', text: opts.userEmail }),
      el('span', { class: 'user-meta', text: `${opts.orgId} · ${opts.role}` }),
    ]),
    buildThemeToggle(callbacks.onThemeChange),
    signOut,
  ]);

  const mapContainer = el('div', { class: 'map-container' });
  const panelContainer = el('div', { class: 'panel-container' });
  // A fourth sibling in the same flex row. Modules swap by toggling `hidden`; the
  // map is never destroyed, because a WebGL context is expensive to rebuild.
  const moduleContainer = el('div', { class: 'module-container', hidden: 'hidden' });

  mapContainer.append(buildLegend());

  root.replaceChildren(
    topbar,
    el('div', { class: 'workspace' }, [rail, mapContainer, panelContainer, moduleContainer]),
  );

  return { mapContainer, panelContainer, moduleContainer, setActiveModule };
}

/**
 * The legend is always visible, not tucked behind a control. A severity ramp the
 * operator has to go looking for is a ramp they will misread.
 */
function buildLegend(): HTMLElement {
  const legend = el('div', { class: 'legend' }, [
    el('h2', { class: 'legend-title', text: 'Severity' }),
    el('ul', { class: 'legend-list' }),
  ]);
  renderLegendItems(legend);
  return legend;
}

/**
 * Counts shown beside each swatch.
 *
 * From the stats endpoint, not from rendered features — see stats.ts for why the
 * map is the wrong thing to count. Held at module scope so a theme flip, which
 * re-runs renderLegendItems, does not wipe them.
 */
let counts: { tiers: number[]; unrated: number; repaired: number } | null = null;

/** Compact mode: swatches only, for a narrow map pane. */
let compact = false;

export function setLegendCounts(
  tiers: number[] | null,
  unrated: number | null,
  repaired: number | null,
): void {
  counts =
    tiers && unrated !== null && repaired !== null ? { tiers, unrated, repaired } : null;
  refreshLegend();
}

/**
 * Fill (or refill) the legend swatches.
 *
 * Split out because the swatch colours are resolved from CSS custom properties
 * at build time — the same reason map.ts has to rebuild its layers — so a theme
 * flip has to re-run this or the legend stops matching the markers.
 */
function renderLegendItems(legend: Element): void {
  const list = legend.querySelector('.legend-list');
  if (!list) return;

  const colors = severityColors();
  const rows: Array<{
    label: string;
    color: string;
    size: number;
    count: number | null;
    /** Circle unless stated: triangle = sensor event, square = camera frame. */
    shape?: 'triangle' | 'square';
    /** Outline only — "this reading reached no cluster". */
    hollow?: boolean;
    /** A low-zoom bin, not a defect. */
    aggregate?: boolean;
  }> =
    SEVERITY_TIERS.map((tier, i) => ({
      label: tier.label,
      color: colors[i] ?? unknownColor(),
      size: tier.radius * 1.4,
      count: counts?.tiers[i] ?? null,
    }));
  rows.push({
    label: UNRATED_LABEL,
    color: unknownColor(),
    size: 9,
    count: counts?.unrated ?? null,
  });
  rows.push({ label: 'Repaired', color: '', size: 12, count: counts?.repaired ?? null });

  // ── What else is actually on screen ──────────────────────────────────────────
  //
  // The legend explained six severity states while about eleven marker states were
  // drawn. Everything below was unexplained: the two raw-detection layers, the
  // aggregate bin, and the hollow convention -- which is the layers' central visual
  // grammar and appeared nowhere.
  //
  // Shape is the primary key now: a circle is a corroborated defect, a triangle one
  // sensor reading, a square one camera image. The swatches say so.
  rows.push({ label: 'Sensor event · pothole', color: cssVar('--review-class-0'), size: 11,
              count: null, shape: 'triangle' });
  rows.push({ label: 'Sensor event · crack', color: cssVar('--review-class-4'), size: 11,
              count: null, shape: 'triangle' });
  rows.push({ label: 'Camera frame', color: cssVar('--color-accent-2'), size: 11,
              count: null, shape: 'square' });
  rows.push({ label: 'Not yet scored', color: cssVar('--marker-neutral'), size: 11,
              count: null, shape: 'square' });
  rows.push({ label: 'Reached no cluster', color: cssVar('--marker-neutral'), size: 11,
              count: null, shape: 'square', hollow: true });
  rows.push({ label: 'Many defects (count shown)', color: unknownColor(), size: 16,
              count: null, aggregate: true });

  const items = rows.map((row) => {
    const isRepaired = row.label === 'Repaired';
    // The swatch has to be the SHAPE the map draws, or the legend is a different
    // key from the one on screen -- which is the failure severity.ts's own header
    // warns about ("three copies of a colour ramp is exactly how a legend silently
    // stops matching its markers"). CSS shapes rather than a shared bitmap: the map's
    // icons are canvas ImageData for MapLibre, and reusing them here would mean
    // rasterising into an <img> for a 11px swatch.
    const shapeClass = row.shape ? ` legend-dot-${row.shape}` : '';
    const hollowClass = row.hollow ? ' legend-dot-hollow' : '';
    const aggregateClass = row.aggregate ? ' legend-dot-aggregate' : '';
    const fill = row.hollow
      ? `border-color:${row.color}`
      : `background:${row.color};border-color:${row.color}`;
    const dot = el('span', {
      class: isRepaired
        ? 'legend-dot legend-dot-repaired'
        : `legend-dot${shapeClass}${hollowClass}${aggregateClass}`,
      style: isRepaired
        ? `width:${row.size}px;height:${row.size}px`
        : `${fill};width:${row.size}px;height:${row.size}px`,
      'aria-hidden': 'true',
      // In compact mode the label is gone, so the swatch has to carry it.
      title: row.count === null ? row.label : `${row.label} — ${row.count}`,
    });
    if (compact) return el('li', { class: 'legend-item' }, [dot]);
    return el('li', { class: 'legend-item' }, [
      dot,
      el('span', { text: row.label }),
      row.count === null
        ? null
        : el('span', { class: 'legend-count', text: String(row.count) }),
    ]);
  });
  list.replaceChildren(...items);
}

/** Re-render the legend swatches from the current theme's tokens. */
export function refreshLegend(root: ParentNode = document): void {
  const legend = root.querySelector('.legend, .legend-strip');
  if (legend) renderLegendItems(legend);
}

/**
 * Swap the legend to its compact strip when the map pane gets narrow, and keep
 * MapLibre's canvas in step with the pane.
 *
 * A ResizeObserver on the pane rather than a media query, because the pane's
 * width depends on whether the detail panel is open — a viewport-width query
 * would leave the full legend overlapping an open panel on a laptop.
 */
const COMPACT_BELOW_PX = 700;

/**
 * Publish the height of MapLibre's bottom-right control stack so the legend can
 * sit clear of it.
 *
 * Both are absolutely positioned in the same corner, and the legend was winning:
 * measured at 1920x889 it spanned y 653-865 and covered the zoom buttons
 * (765-823) outright -- Playwright reported `.legend intercepts pointer events`
 * when asked to click zoom-in -- as well as the attribution strip (843-867).
 * The second of those is not cosmetic: the OSM/Protomaps attribution has to stay
 * visible to satisfy the basemap licence.
 *
 * Measured rather than hardcoded because the stack's height is not constant --
 * the attribution wraps to two lines on a narrow pane, and the compact/expanded
 * attribution toggle changes it at runtime.
 */
function publishControlStackHeight(mapContainer: HTMLElement): void {
  const stack = mapContainer.querySelector('.maplibregl-ctrl-bottom-right');
  // Fallback keeps the legend clear on the first frame, before MapLibre has
  // added its controls.
  const height = stack ? Math.ceil(stack.getBoundingClientRect().height) : 116;
  mapContainer.style.setProperty('--map-ctrl-bottom-h', `${height}px`);
}

export function installLegendResponsiveness(
  mapContainer: HTMLElement,
  onResize: () => void,
): () => void {
  publishControlStackHeight(mapContainer);
  const observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width ?? mapContainer.clientWidth;
    // A hidden pane is not a layout state to react to, it is an absent one. Switching
    // to another module sets `hidden`, which reports width 0 — and every line below
    // then misbehaves: the legend flips to its compact strip, the control-stack height
    // is measured off a display:none element as 0, and map.resize() sizes the canvas
    // to 0x0. All of it would have to be undone on the way back. The guard wraps the
    // WHOLE callback for that reason, not just the compact flip.
    if (width === 0 || !mapContainer.isConnected) return;
    const next = width < COMPACT_BELOW_PX;
    if (next !== compact) {
      compact = next;
      const legend = mapContainer.querySelector('.legend, .legend-strip');
      if (legend) {
        legend.className = compact ? 'legend-strip' : 'legend';
        const title = legend.querySelector('.legend-title');
        if (title) (title as HTMLElement).hidden = compact;
        renderLegendItems(legend);
      }
    }
    // MapLibre does not watch its container, so without this the canvas keeps the
    // old size and the map appears stretched when the panel opens.
    onResize();
    publishControlStackHeight(mapContainer);
  });
  observer.observe(mapContainer);

  // The controls are added asynchronously by MapLibre, after this runs, and the
  // attribution can re-wrap without the pane resizing -- so a container resize
  // is not the only thing that changes the stack height.
  const stack = mapContainer.querySelector('.maplibregl-ctrl-bottom-right');
  const stackObserver = stack
    ? new ResizeObserver(() => publishControlStackHeight(mapContainer))
    : null;
  if (stack && stackObserver) stackObserver.observe(stack);

  return () => {
    observer.disconnect();
    stackObserver?.disconnect();
  };
}
