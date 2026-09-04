/**
 * The floating dock: KPIs, filters, and the hover readout.
 *
 * Structure, order and strings follow the source mockup (`RoadWatch Dashboard.dc.html`,
 * recoverable with `git show`): provenance note, then a card carrying an eyebrow +
 * scope heading, a 2×2 KPI grid, a divider, then Find a street → Severity tier →
 * Detection source → Corroboration. `organic-shell.css` already styles every class
 * used here, so this file emits markup rather than inventing layout.
 *
 * ## Elements the schema cannot fully back
 *
 * The mockup shows figures this database has no way to produce. They are rendered in
 * their designed position — the layout is the design's — but never as a fabricated
 * number, because a city may act on what this screen says. `provisional()` marks
 * every one of them the same way, so they are greppable rather than five ad-hoc
 * em-dashes:
 *
 *   - **KPI deltas** ("−214 this month"). No month-ago baseline exists to compute
 *     from: `asset_cluster.updated_at` is rewritten by every clustering pass.
 *   - **Find a street.** There is no street column anywhere in the schema, so the
 *     input renders disabled and says why.
 *
 * Detection-source chips ARE backed — `/clusters/stats` returns the real mix — but
 * the clustering job only ever writes `'crowd'`, so in practice the row shows one
 * chip. That is the truth about the data, and showing it is more informative than
 * hiding the row.
 */

import { el, formatNumber } from './dom.ts';
import { SEVERITY_TIERS, UNRATED_LABEL } from './severity.ts';
import type { ClusterStats } from './stats.ts';

/** Corroboration steps, matching the mockup. Single-select, like a radio group. */
/**
 * Zoom floor shown in the raw-observations hint. Mirrors OBSERVATIONS_MIN_ZOOM
 * in map/layers.ts, which in turn mirrors `tile_observations_min_zoom` in
 * app/config.py. Duplicated as a literal rather than imported so the dock does
 * not pull in the map module for one number.
 */
const OBSERVATIONS_HINT_ZOOM = 15;

const DEVICE_STEPS = [
  { value: 1, label: 'Any' },
  { value: 2, label: '2+ devices' },
  { value: 4, label: '4+ devices' },
];

const ALL_TIER_LABELS = [...SEVERITY_TIERS.map((t) => t.label), UNRATED_LABEL];

/**
 * Shown above the card when the data on screen is a fixture rather than a real
 * collection. Set `VITE_PROVENANCE_NOTE` to the text for a demo build; unset in a
 * real deployment, where the note would be a lie.
 */
const PROVENANCE_NOTE = import.meta.env['VITE_PROVENANCE_NOTE'] ?? '';

const NO_BASELINE =
  'No historical baseline yet — asset_cluster.updated_at is rewritten by every ' +
  'clustering pass, so there is no month-ago state to compare against.';

export interface DockFilter {
  tiers: Set<string>;
  sources: Set<string>;
  minDevices: number;
  /**
   * Which sensor classes the observations layer draws.
   *
   * Defaults to pothole alone. Only pothole-classed readings are ever eligible for
   * cluster membership, so the other 94.6% of the corpus cannot become a defect no
   * matter what — showing them all by default buried the 254 readings that fed
   * something under 5,432 that could not.
   */
  observationClasses: Set<string>;
}

/** Chip labels for the three sensor classes. Keys match OBSERVATION_CLASSES. */
const CLASS_LABELS: [string, string][] = [
  ['pothole', 'Pothole'],
  ['crack', 'Crack'],
  ['other', 'Other'],
];

export interface DockCallbacks {
  onFilterChange: (filter: DockFilter) => void;
  /** Raw-observation layer toggled. Separate from onFilterChange because it is a
   * layer visibility change, not a filter over the cluster source. */
  onObservationsToggle: (visible: boolean) => void;
  /** Camera-frame layer toggled. */
  onFramesToggle: (visible: boolean) => void;
}

/**
 * A value the data cannot supply. An em-dash plus the reason on hover — never a
 * plausible-looking number.
 */
function provisional(reason: string): HTMLElement {
  return el('span', { class: 'kpi-delta is-provisional', text: '—', title: reason });
}

export class Dock {
  private readonly root: HTMLElement;
  private readonly scroll: HTMLElement;
  private readonly collapsed: HTMLButtonElement;
  private readonly heading: HTMLElement;
  private readonly kpiGrid: HTMLElement;
  private readonly tierRow: HTMLElement;
  private readonly classRow: HTMLElement;
  private readonly classGroup: HTMLElement;
  private readonly sourceRow: HTMLElement;
  private readonly sourceGroup: HTMLElement;
  private readonly deviceRow: HTMLElement;
  private readonly readout: HTMLElement;
  private observationsToggle!: HTMLInputElement;
  private framesToggle!: HTMLInputElement;
  private observationsHint!: HTMLElement;
  private readonly note: HTMLElement;

  private readonly filter: DockFilter = {
    tiers: new Set(ALL_TIER_LABELS),
    sources: new Set(),
    minDevices: 1,
    observationClasses: new Set(['pothole']),
  };
  private stats: ClusterStats | null = null;
  /** Every source seen in the viewport, so "all selected" can be recognised. */
  private knownSources: string[] = [];
  private zoomHint = '';

  constructor(
    container: HTMLElement,
    opts: { scope: string },
    private readonly callbacks: DockCallbacks,
  ) {
    this.heading = el('h2', { class: 'dock-title', text: opts.scope });
    this.kpiGrid = el('div', { class: 'kpi-grid' });
    this.tierRow = el('div', { class: 'chip-row' });
    this.classRow = el('div', { class: 'chip-row' });
    this.sourceRow = el('div', { class: 'chip-row' });
    this.deviceRow = el('div', { class: 'chip-row' });
    this.note = el('p', { class: 'provenance-note', hidden: 'hidden' });

    this.readout = el('div', { class: 'dock-readout' }, [
      el('span', { text: 'Hover a marker for its reading · click to open the record' }),
    ]);

    // Icon-only, so the label describes the action rather than the state.
    const collapse = el('button', {
      class: 'icon-button dock-collapse',
      type: 'button',
      text: '‹',
      'aria-label': 'Collapse filters',
      title: 'Collapse filters',
      'aria-expanded': 'true',
    });
    collapse.addEventListener('click', () => this.setOpen(false));

    // There is no street column in the schema, so this cannot work. Rendered
    // rather than dropped because the mockup's layout allows for it, and disabled
    // with the reason so nobody files it as a bug.
    const streetSearch = el('input', {
      class: 'input',
      type: 'search',
      disabled: true,
      placeholder: 'Street search needs a street column',
      'aria-label': 'Find a street (unavailable)',
      title: 'No street data: clusters carry coordinates only, not a street name.',
    });

    this.sourceGroup = el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Detection source' }),
      this.sourceRow,
    ]);

    // Raw observations. Off by default -- see PotholeMap.observationsVisible.
    // The hint carries the zoom floor because the layer is server-gated at z15
    // and an operator toggling it on at z13 would otherwise see nothing happen
    // and reasonably conclude it was broken.
    this.observationsToggle = el('input', {
      type: 'checkbox',
      id: 'toggle-observations',
      class: 'dock-toggle-input',
    }) as HTMLInputElement;
    this.observationsToggle.addEventListener('change', () => {
      this.classGroup.hidden = !this.observationsToggle.checked;
      this.callbacks.onObservationsToggle(this.observationsToggle.checked);
      // Re-emit so the class filter is applied to a layer that may have only just
      // been created. Without this the default (pothole only) would not take effect
      // until the operator happened to click a chip.
      this.emit();
    });
    this.observationsHint = el('p', {
      class: 'dock-group-hint',
      text:
        'Individual readings before clustering. Solid means it fed a defect; hollow ' +
        'means it fed nothing — most readings do not, because only pothole-classed ' +
        `ones are eligible. Zoom ${OBSERVATIONS_HINT_ZOOM}+ to see them.`,
    });
    // Camera frames. A separate toggle rather than one "show raw data" switch,
    // because the two answer different questions -- where a wheel hit something
    // versus where the camera thought it saw something -- and 10,000 points from
    // both at once is unreadable.
    this.framesToggle = el('input', {
      type: 'checkbox',
      id: 'toggle-frames',
      class: 'dock-toggle-input',
    }) as HTMLInputElement;
    this.framesToggle.addEventListener('change', () => {
      this.callbacks.onFramesToggle(this.framesToggle.checked);
    });

    // Nested under the sensor toggle and hidden until it is on, because it filters
    // that layer and nothing else. No counts on these chips: a per-class count would
    // have to come from the observations tile, which is zoom-gated and per-tile
    // capped, so the number would disagree with what is drawn. An honest label beats
    // a count that is wrong at 4 zoom levels out of 5.
    this.classGroup = el('div', { class: 'dock-subgroup', hidden: 'hidden' }, [
      el('h4', { class: 'dock-subgroup-title', text: 'Class' }),
      this.classRow,
    ]);

    const observationsGroup = el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Raw detections' }),
      el('label', { class: 'dock-toggle' }, [
        this.observationsToggle,
        el('span', { text: 'Sensor observations' }),
      ]),
      this.classGroup,
      el('label', { class: 'dock-toggle' }, [
        this.framesToggle,
        el('span', { text: 'Camera frames' }),
      ]),
      this.observationsHint,
    ]);

    const card = el('div', { class: 'dock-card' }, [
      el('div', { class: 'dock-card-header' }, [
        el('div', { class: 'dock-heading-block' }, [
          el('span', { class: 'dock-eyebrow', text: 'Network health' }),
          this.heading,
        ]),
        collapse,
      ]),
      this.kpiGrid,
      el('div', { class: 'dock-divider' }),
      el('div', { class: 'dock-group' }, [
        el('h3', { class: 'dock-group-title', text: 'Find a street' }),
        streetSearch,
      ]),
      el('div', { class: 'dock-group' }, [
        el('h3', { class: 'dock-group-title', text: 'Severity tier' }),
        this.tierRow,
      ]),
      this.sourceGroup,
      el('div', { class: 'dock-group' }, [
        el('h3', { class: 'dock-group-title', text: 'Corroboration' }),
        this.deviceRow,
      ]),
      observationsGroup,
    ]);

    this.scroll = el('div', { class: 'dock-scroll' }, [this.note, card]);

    this.collapsed = el('button', {
      class: 'dock-collapsed',
      type: 'button',
      hidden: 'hidden',
      'aria-expanded': 'false',
    }) as HTMLButtonElement;
    this.collapsed.append(
      el('span', { class: 'dock-collapsed-caret', text: '›', 'aria-hidden': 'true' }),
      el('span', { text: 'Network health & filters' }),
    );
    this.collapsed.addEventListener('click', () => this.setOpen(true));

    // The readout is the dock's last row and stays visible when collapsed — an
    // operator scrubbing the map still needs the reading under the cursor.
    this.root = el('aside', { class: 'dock', 'aria-label': 'Filters and totals' }, [
      this.scroll,
      this.collapsed,
      this.readout,
    ]);
    container.append(this.root);

    this.renderNote();
    this.renderChips();
    this.renderKpis();
  }

  private setOpen(open: boolean): void {
    this.scroll.hidden = !open;
    this.collapsed.hidden = open;
  }

  /** Fold in a fresh stats payload. */
  update(stats: ClusterStats): void {
    this.stats = stats;
    // Coalesced rather than trusted: a server that predates source_counts would
    // otherwise throw here and take every count on the card down with it, which
    // is a worse failure than one missing filter row.
    const sources = Object.keys(stats.source_counts ?? {}).sort();
    // Default to every source selected. Only newly-appearing sources are added, so
    // panning does not silently re-enable one the operator switched off.
    for (const source of sources) {
      if (!this.knownSources.includes(source)) this.filter.sources.add(source);
    }
    this.knownSources = sources;
    this.renderKpis();
    this.renderChips();
  }

  /** Called when the stats request fails — say so rather than showing stale numbers. */
  setUnavailable(): void {
    this.stats = null;
    this.renderKpis();
    this.renderChips();
  }

  /**
   * Below the aggregate zoom the tiles carry no severity, source or device count,
   * so the chips genuinely cannot act. Say that rather than letting them look broken.
   */
  setFiltersApply(apply: boolean): void {
    this.zoomHint = apply
      ? ''
      : 'Filters apply at street zoom — grouped markers carry no severity or device count.';
    this.renderNote();
    for (const chip of this.root.querySelectorAll<HTMLButtonElement>('.chip')) {
      chip.disabled = !apply;
    }
  }

  setReadout(properties: Record<string, unknown> | null): void {
    const span = this.readout.querySelector('span:last-child');
    if (!span) return;
    if (!properties) {
      span.textContent = 'Hover a marker for its reading · click to open the record';
      return;
    }
    const severity = typeof properties['severity'] === 'number' ? properties['severity'] : null;
    span.textContent = [
      properties['repaired'] ? 'repaired' : 'open',
      severity === null ? 'unrated' : `severity ${formatNumber(severity, 2)}`,
      // Passes, not just devices: one car over the same defect on three days is
      // three surveys in the source paper and one device here. Showing devices
      // alone made every real cluster read as uncorroborated.
      `${properties['distinct_devices'] ?? 0} dev · ${properties['distinct_passes'] ?? 0} pass`,
      `${properties['observation_count'] ?? 0} observations`,
    ].join(' · ');
  }

  /** Open clusters passing the tier filter, from SQL counts rather than the map. */
  shownCount(): number {
    if (!this.stats) return 0;
    let total = 0;
    for (const [i, tier] of SEVERITY_TIERS.entries()) {
      if (this.filter.tiers.has(tier.label)) total += this.stats.tier_counts[i] ?? 0;
    }
    if (this.filter.tiers.has(UNRATED_LABEL)) total += this.stats.unrated;
    return total;
  }

  private renderNote(): void {
    const lines = [PROVENANCE_NOTE, this.zoomHint].filter((line) => line !== '');
    this.note.hidden = lines.length === 0;
    this.note.replaceChildren(...lines.map((line) => el('span', { text: line })));
  }

  private renderKpis(): void {
    const s = this.stats;
    const severe = s ? (s.tier_counts[SEVERITY_TIERS.length - 1] ?? 0) : 0;
    const kpis: Array<{ value: string; label: string }> = [
      { value: s ? String(s.open) : '—', label: 'Open defects in view' },
      {
        // "Open" counts clusters; this counts the ones the PUBLIC api would serve.
        // Forming a cluster takes one reading (cluster_min_points = 1); being
        // publishable takes corroboration -- two devices or three passes. The console
        // showed only the first number, so it reported N defects while /potholes
        // would have served none of them. Shown as a ratio because the gap IS the
        // information: 0 of 204 says something 0 alone does not.
        value: s ? `${s.corroborated} of ${s.open}` : '—',
        label: 'Corroborated',
      },
      {
        // Percentages of a tiny denominator mislead, so below 10 open clusters the
        // share is withheld rather than rounded into false confidence.
        value: s && s.open >= 10 ? `${Math.round((severe / s.open) * 100)}%` : '—',
        label: 'Rated severe',
      },
      { value: s ? formatNumber(s.mean_confidence, 2) : '—', label: 'Mean confidence' },
      { value: s ? String(s.repaired_last_30d) : '—', label: 'Repaired this month' },
    ];
    this.kpiGrid.replaceChildren(
      ...kpis.map((kpi) =>
        el('div', { class: 'kpi' }, [
          el('span', { class: 'kpi-value', text: kpi.value }),
          el('span', { class: 'kpi-label', text: kpi.label }),
          // The mockup's delta line. Kept as a slot so the card holds its designed
          // three-line shape, but never filled with an invented figure.
          provisional(NO_BASELINE),
        ]),
      ),
    );
  }

  private renderChips(): void {
    this.tierRow.replaceChildren(
      ...ALL_TIER_LABELS.map((label, i) => {
        const count = this.stats
          ? i < SEVERITY_TIERS.length
            ? (this.stats.tier_counts[i] ?? 0)
            : this.stats.unrated
          : null;
        return this.chip(label, this.filter.tiers.has(label), count, () => {
          // Toggling to empty would blank the map with no way back except
          // re-selecting, so the last active tier stays on.
          if (this.filter.tiers.has(label) && this.filter.tiers.size === 1) return;
          this.toggle(this.filter.tiers, label);
          this.emit();
        });
      }),
    );

    this.classRow.replaceChildren(
      ...CLASS_LABELS.map(([key, label]) =>
        this.chip(label, this.filter.observationClasses.has(key), null, () => {
          // Same guard as the tiers: emptying the set would blank the layer with
          // no way back except re-selecting.
          if (
            this.filter.observationClasses.has(key) &&
            this.filter.observationClasses.size === 1
          ) {
            return;
          }
          this.toggle(this.filter.observationClasses, key);
          this.emit();
        }),
      ),
    );

    // Hidden entirely when the viewport has no clusters — an empty filter row is
    // worse than no row.
    this.sourceGroup.hidden = this.knownSources.length === 0;
    this.sourceRow.replaceChildren(
      ...this.knownSources.map((source) =>
        this.chip(
          source,
          this.filter.sources.has(source),
          this.stats?.source_counts?.[source] ?? null,
          () => {
            if (this.filter.sources.has(source) && this.filter.sources.size === 1) return;
            this.toggle(this.filter.sources, source);
            this.emit();
          },
          true,
        ),
      ),
    );

    this.deviceRow.replaceChildren(
      ...DEVICE_STEPS.map((step) =>
        this.chip(
          step.label,
          this.filter.minDevices === step.value,
          null,
          () => {
            this.filter.minDevices = step.value;
            this.emit();
          },
          true,
        ),
      ),
    );
  }

  private toggle(set: Set<string>, key: string): void {
    if (set.has(key)) set.delete(key);
    else set.add(key);
  }

  private chip(
    label: string,
    active: boolean,
    count: number | null,
    onClick: () => void,
    secondary = false,
  ): HTMLElement {
    const chip = el('button', {
      class: secondary ? 'chip is-secondary' : 'chip',
      type: 'button',
      'aria-pressed': active ? 'true' : 'false',
    });
    // The mockup separates label from count with a middot, not a space.
    chip.append(el('span', { text: count === null ? label : `${label} · ${count}` }));
    chip.addEventListener('click', onClick);
    return chip;
  }

  private emit(): void {
    this.renderChips();
    this.callbacks.onFilterChange({
      tiers: new Set(this.filter.tiers),
      sources: new Set(this.filter.sources),
      minDevices: this.filter.minDevices,
      observationClasses: new Set(this.filter.observationClasses),
    });
  }

  /** True when every known source is selected, i.e. the source filter is inert. */
  allSourcesSelected(): boolean {
    return this.knownSources.every((s) => this.filter.sources.has(s));
  }

  destroy(): void {
    this.root.remove();
  }
}
