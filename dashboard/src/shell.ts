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
import {
  BASEMAPS,
  currentBasemap,
  isBasemapId,
  setStoredBasemap,
  type BasemapId,
} from './map/basemaps.ts';

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
  /** Fired after the operator picks a basemap. The preference is already stored
   * and `data-basemap` already set by the time this runs, so the handler only has
   * to re-style the map. */
  onBasemapChange: (basemap: BasemapId) => void;
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

  // Rendered from docs/guides/operator-console.md by scripts/build-guide.mjs, which
  // runs on prebuild. Relative so it resolves under the /dashboard mount without
  // hardcoding it; a new tab so an operator never loses a half-finished review pass
  // to a navigation.
  const help = el('a', {
    class: 'link-button',
    href: 'guide.html',
    target: '_blank',
    rel: 'noopener',
    title: 'How to use this console',
    text: 'Help',
  });

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
    help,
    buildThemeToggle(callbacks.onThemeChange),
    signOut,
  ]);

  const mapContainer = el('div', { class: 'map-container' });
  const panelContainer = el('div', { class: 'panel-container' });
  // A fourth sibling in the same flex row. Modules swap by toggling `hidden`; the
  // map is never destroyed, because a WebGL context is expensive to rebuild.
  const moduleContainer = el('div', { class: 'module-container', hidden: 'hidden' });

  mapContainer.append(buildMapControls(callbacks.onBasemapChange));

  root.replaceChildren(
    topbar,
    el('div', { class: 'workspace' }, [rail, mapContainer, panelContainer, moduleContainer]),
  );

  return { mapContainer, panelContainer, moduleContainer, setActiveModule };
}

/**
 * The top-right overlay column: the severity legend (or its collapsed pill), with
 * the basemap switcher beneath it.
 *
 * A wrapper rather than two absolutely-positioned siblings, and that is the whole
 * design. installLegendResponsiveness swaps the legend's ENTIRE className between
 * 'legend' and 'legend-strip', so anything the legend must keep across that swap
 * cannot be a class. Position therefore lives on this wrapper, and open/closed is
 * the `hidden` ATTRIBUTE — which a className assignment cannot touch. That is the
 * same mechanism Dock.setOpen uses, for a related reason.
 *
 * The legend is no longer always visible, which reverses the note this function
 * used to carry ("a severity ramp the operator has to go looking for is a ramp
 * they will misread"). That was written when the legend was six rows; Phase 2.11
 * grew it to fifteen across three sections, and a permanent block that size is its
 * own legibility problem. It still opens by default, and the pill keeps the word
 * "Severity" on screen so there is something to go looking for.
 */
function buildMapControls(onBasemapChange: (id: BasemapId) => void): HTMLElement {
  // Icon-only, so the label and title describe the ACTION rather than the state —
  // the convention buildThemeToggle follows.
  const collapse = el('button', {
    class: 'icon-button legend-collapse',
    type: 'button',
    text: '›',
    'aria-label': 'Collapse the severity legend',
    title: 'Collapse the severity legend',
    'aria-expanded': 'true',
  });

  const legend = el('div', { class: 'legend' }, [
    el('div', { class: 'legend-header' }, [
      el('h2', { class: 'legend-title', text: 'Severity' }),
      collapse,
    ]),
    el('ul', { class: 'legend-list' }),
  ]);
  renderLegendItems(legend);

  const pill = el('button', {
    class: 'legend-collapsed',
    type: 'button',
    hidden: 'hidden',
    'aria-expanded': 'false',
    'aria-label': 'Show the severity legend',
  });
  pill.append(
    el('span', { class: 'legend-collapsed-caret', text: '‹', 'aria-hidden': 'true' }),
    el('span', { text: 'Severity' }),
  );

  const setOpen = (open: boolean): void => {
    legend.hidden = !open;
    pill.hidden = open;
    collapse.setAttribute('aria-expanded', String(open));
    pill.setAttribute('aria-expanded', String(open));
    storeLegendOpen(open);
  };
  collapse.addEventListener('click', () => setOpen(false));
  pill.addEventListener('click', () => {
    setOpen(true);
    // Focus follows the disclosure, or a keyboard operator is left standing on a
    // button that just became display:none.
    collapse.focus();
  });

  const controls = el('div', { class: 'map-controls' }, [
    legend,
    pill,
    buildBasemapControl(onBasemapChange),
  ]);
  // Applied rather than set as initial attributes, so one function is the single
  // writer of both elements' visibility and they cannot disagree.
  setOpen(storedLegendOpen());
  return controls;
}

/**
 * Whether the legend is open.
 *
 * Remembered for the same reason the review module remembers its keyboard legend
 * (review.ts): an operator should state a preference like this once. Deliberately
 * INDEPENDENT of `compact` — that is layout-driven and chooses card vs strip,
 * while this is operator-driven and chooses legend vs pill. Auto-collapsing on a
 * narrow pane would overwrite an explicit choice with a transient layout event,
 * and the detail panel opening and closing would flap it.
 */
const LEGEND_OPEN_KEY = 'roadwatch.legend';

function storedLegendOpen(): boolean {
  try {
    return (localStorage.getItem(LEGEND_OPEN_KEY) ?? '1') === '1';
  } catch {
    // Private browsing or a blocked origin — open, as on a first visit.
    return true;
  }
}

function storeLegendOpen(open: boolean): void {
  try {
    localStorage.setItem(LEGEND_OPEN_KEY, open ? '1' : '0');
  } catch {
    // Preference simply won't survive a reload; not worth failing the click.
  }
}

/**
 * The basemap picker.
 *
 * A native <select>, following the .asset-select precedent: seven options are
 * keyboard- and screen-reader-correct for free, with no popover to position,
 * dismiss or focus-trap. A custom listbox for seven static items would be more
 * bug surface than component.
 *
 * Not collapsible with the legend. It is one row, and an operator who wants
 * imagery should not first have to find the legend in order to un-hide it.
 */
function buildBasemapControl(onChange: (id: BasemapId) => void): HTMLElement {
  const select = el('select', { class: 'basemap-select', 'aria-label': 'Basemap' });
  for (const option of BASEMAPS) {
    select.append(
      el('option', {
        value: option.id,
        selected: option.id === currentBasemap(),
        text: option.label,
      }),
    );
  }
  select.addEventListener('change', () => {
    // A <select> yields a string; the guard is what keeps an unknown value from
    // reaching namedFlavor(), which throws rather than falling back.
    if (!isBasemapId(select.value)) return;
    // Stores the preference AND sets `data-basemap` — which must happen before the
    // map re-styles, because addClusterLayers re-reads --marker-halo from it.
    setStoredBasemap(select.value);
    onChange(select.value);
  });
  // .legend-title reuses the terracotta small-caps eyebrow, so the two cards read
  // as one column rather than two unrelated boxes.
  return el('div', { class: 'basemap-control' }, [
    el('h2', { class: 'legend-title', text: 'Basemap' }),
    select,
  ]);
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
    /** Circle unless stated: triangle = sensor reading, square = camera frame. */
    shape?: 'triangle' | 'square';
    /** Outline only — "this contributed to nothing". */
    hollow?: boolean;
    /** A low-zoom bin, not a defect. */
    aggregate?: boolean;
    /** Starts a new labelled section, rendered before this row. */
    group?: string;
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
  // Everything above is severity; everything below is a different question, and
  // running them together under one "Severity" heading is a large part of why the
  // map read as confused. Two labelled sections now, and the holes are filled:
  //
  //   * `not`-classed readings had NO row at all, while being 31% of the corpus —
  //     1,781 grey triangles an operator could only guess at.
  //   * Grey meant two unrelated things (a `not` reading, an unscored frame) with
  //     only the frame meaning explained, so a grey triangle read as "not yet
  //     scored". Every observation is scored; there are zero unscored ones.
  //   * "Reached no cluster" was drawn as a square and was FALSE for triangles,
  //     where hollow used to mean outlier-flagged. Now one rule covers all three
  //     shapes, so it is stated once, for all of them.
  rows.push({ group: 'What it is', label: 'Sensor reading · pothole',
              color: cssVar('--review-class-0'), size: 11, count: null, shape: 'triangle' });
  rows.push({ label: 'Sensor reading · crack', color: cssVar('--review-class-4'), size: 11,
              count: null, shape: 'triangle' });
  rows.push({ label: 'Sensor reading · other', color: cssVar('--marker-neutral'), size: 11,
              count: null, shape: 'triangle' });
  rows.push({ label: 'Camera frame', color: cssVar('--color-accent-2'), size: 11,
              count: null, shape: 'square' });
  rows.push({ label: 'Camera frame · not yet scored', color: cssVar('--marker-neutral'),
              size: 11, count: null, shape: 'square' });
  rows.push({ label: 'Many defects (count shown)', color: unknownColor(), size: 16,
              count: null, aggregate: true });

  // One rule, three shapes. A hollow circle is the same statement one level up:
  // the defect exists but nothing has corroborated it.
  rows.push({ group: 'Hollow means', label: 'Defect · not yet corroborated',
              color: unknownColor(), size: 12, count: null, hollow: true });
  rows.push({ label: 'Reading · fed no defect', color: cssVar('--marker-neutral'), size: 11,
              count: null, shape: 'triangle', hollow: true });
  rows.push({ label: 'Frame · paired with nothing', color: cssVar('--marker-neutral'),
              size: 11, count: null, shape: 'square', hollow: true });

  const items = rows.flatMap((row) => {
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
    // One custom property rather than background/border-color inline, so the CSS
    // can decide how each shape expresses "hollow". A clip-path triangle has no
    // border to leave showing, so it needs a second inner shape — which inline
    // styles cannot express, and which used to make a hollow triangle invisible.
    const fill = `--legend-swatch:${row.color}`;
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
    // In compact mode the labels are gone, so a section heading would be a stray
    // word above unlabelled swatches. The titles still carry the meaning there.
    const heading =
      row.group && !compact
        ? el('li', { class: 'legend-section' }, [el('span', { text: row.group })])
        : null;

    const item = compact
      ? el('li', { class: 'legend-item' }, [dot])
      : el('li', { class: 'legend-item' }, [
          dot,
          el('span', { text: row.label }),
          row.count === null
            ? null
            : el('span', { class: 'legend-count', text: String(row.count) }),
        ]);
    return heading ? [heading, item] : [item];
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
    // A hidden pane is not a layout state to react to, it is an absent one. Switching
    // to another module sets `hidden`, which reports width 0 — and every line below
    // then misbehaves: the legend flips to its compact strip, and map.resize() sizes
    // the canvas to 0x0. All of it would have to be undone on the way back. The guard
    // wraps the WHOLE callback for that reason, not just the compact flip.
    if (width === 0 || !mapContainer.isConnected) return;
    const next = width < COMPACT_BELOW_PX;
    if (next !== compact) {
      compact = next;
      const legend = mapContainer.querySelector('.legend, .legend-strip');
      if (legend) {
        // A whole-className assignment: nothing else may live on this element's
        // class list. Position belongs to .map-controls and open/closed is the
        // `hidden` attribute on this element and its pill sibling, for that reason.
        // Hiding the title in strip mode is CSS's job now, not this function's.
        legend.className = compact ? 'legend-strip' : 'legend';
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
