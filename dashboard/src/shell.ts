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

const MODULES = [
  { id: 'map', label: 'Map', glyph: 'M', enabled: true },
  { id: 'inventory', label: 'Inventory', glyph: 'I', enabled: false },
  { id: 'work-orders', label: 'Work orders', glyph: 'W', enabled: false },
  { id: 'reports', label: 'Reports', glyph: 'R', enabled: false },
  { id: 'admin', label: 'Admin', glyph: 'A', enabled: false },
];

export interface UrlState {
  asset: string;
  view: MapView | null;
  cluster: string | null;
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
  return {
    asset: params.get('asset') ?? 'pothole',
    view: lat !== null && lon !== null && zoom !== null ? { lat, lon, zoom } : null,
    cluster: params.get('cluster'),
  };
}

export function writeUrlState(state: UrlState): void {
  const params = new URLSearchParams();
  params.set('asset', state.asset);
  if (state.view) {
    params.set('z', state.view.zoom.toFixed(2));
    params.set('lat', state.view.lat.toFixed(6));
    params.set('lon', state.view.lon.toFixed(6));
  }
  if (state.cluster) params.set('cluster', state.cluster);
  const next = `#/${params.toString()}`;
  if (next !== location.hash) history.replaceState(null, '', next);
}

export interface ShellCallbacks {
  onAssetTypeChange: (assetType: string) => void;
  onSignOut: () => void;
  /** Fired after the theme flips, so the map can repaint its layers. */
  onThemeChange: (theme: Theme) => void;
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
): { mapContainer: HTMLElement; panelContainer: HTMLElement } {
  const rail = el('nav', { class: 'rail', 'aria-label': 'Modules' });
  for (const module of MODULES) {
    const button = el('button', {
      class: module.enabled ? 'rail-item is-active' : 'rail-item is-disabled',
      type: 'button',
      disabled: !module.enabled,
      title: module.enabled ? module.label : `${module.label} — not yet available`,
      'aria-label': module.label,
    });
    button.append(
      el('span', { class: 'rail-glyph', text: module.glyph, 'aria-hidden': 'true' }),
      el('span', { class: 'rail-label', text: module.label }),
    );
    rail.append(button);
  }

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

  mapContainer.append(buildLegend());

  root.replaceChildren(
    topbar,
    el('div', { class: 'workspace' }, [rail, mapContainer, panelContainer]),
  );

  return { mapContainer, panelContainer };
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
  const rows: Array<{ label: string; color: string; size: number; count: number | null }> =
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

  const items = rows.map((row) => {
    const isRepaired = row.label === 'Repaired';
    const dot = el('span', {
      class: isRepaired ? 'legend-dot legend-dot-repaired' : 'legend-dot',
      style: isRepaired
        ? `width:${row.size}px;height:${row.size}px`
        : `background:${row.color};width:${row.size}px;height:${row.size}px`,
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

export function installLegendResponsiveness(
  mapContainer: HTMLElement,
  onResize: () => void,
): () => void {
  const observer = new ResizeObserver((entries) => {
    const width = entries[0]?.contentRect.width ?? mapContainer.clientWidth;
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
  });
  observer.observe(mapContainer);
  return () => observer.disconnect();
}
