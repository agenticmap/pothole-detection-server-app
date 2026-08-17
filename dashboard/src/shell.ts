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
import { SEVERITY_TIERS, severityColors } from './severity.ts';
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
      el('span', { class: 'brand-mark', 'aria-hidden': 'true', text: '◆' }),
      el('span', { class: 'brand-name', text: 'RoadWatch' }),
    ]),
    assetSelect,
    el('div', { class: 'topbar-spacer' }),
    el('div', { class: 'user-block' }, [
      el('span', { class: 'user-email', text: opts.userEmail }),
      el('span', { class: 'user-meta', text: `${opts.orgId} · ${opts.role}` }),
    ]),
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
  const colors = severityColors();
  const items = SEVERITY_TIERS.map((tier, i) =>
    el('li', { class: 'legend-item' }, [
      el('span', {
        class: 'legend-dot',
        style: `background:${colors[i]};width:${tier.radius * 1.4}px;height:${tier.radius * 1.4}px`,
        'aria-hidden': 'true',
      }),
      el('span', { text: tier.label }),
    ]),
  );
  items.push(
    el('li', { class: 'legend-item' }, [
      el('span', { class: 'legend-dot legend-dot-repaired', 'aria-hidden': 'true' }),
      el('span', { text: 'Repaired' }),
    ]),
  );
  return el('div', { class: 'legend' }, [
    el('h2', { class: 'legend-title', text: 'Severity' }),
    el('ul', { class: 'legend-list' }, items),
  ]);
}
