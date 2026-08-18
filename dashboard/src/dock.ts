/**
 * The floating dock: KPIs, severity and corroboration filters, and the hover
 * readout.
 *
 * Structure and class names come from the design handoff — organic-shell.css
 * already styles every selector used here, so this file emits markup rather than
 * inventing layout.
 *
 * What is deliberately NOT here, and why:
 *
 *   - **KPI delta lines** ("−214 this month"). There is no baseline to compute
 *     them from: `asset_cluster.updated_at` is rewritten by every clustering
 *     pass, so no month-ago state survives. The mockup's deltas are placeholder
 *     text, and the handoff says to wire them to a real query or drop them.
 *   - **A street name / "Find a street" box.** No street column exists anywhere
 *     in the schema. It would need reverse geocoding and a migration.
 *   - **Detection-source chips.** `asset_cluster.source` is hardcoded 'crowd' by
 *     the clustering job, so the row would read "crowd · 100%".
 *
 * A dashboard a city makes repair decisions with should not show a number it
 * cannot stand behind, so these are absent rather than faked.
 */

import { el, formatNumber } from './dom.ts';
import { SEVERITY_TIERS, UNRATED_LABEL } from './severity.ts';
import type { ClusterStats } from './stats.ts';

/** Corroboration steps, matching the mockup. Single-select, like a radio group. */
const DEVICE_STEPS = [
  { value: 1, label: 'Any' },
  { value: 2, label: '2+ devices' },
  { value: 4, label: '4+ devices' },
];

const ALL_TIER_LABELS = [...SEVERITY_TIERS.map((t) => t.label), UNRATED_LABEL];

export interface DockFilter {
  tiers: Set<string>;
  minDevices: number;
}

export interface DockCallbacks {
  onFilterChange: (filter: DockFilter) => void;
}

export class Dock {
  private readonly root: HTMLElement;
  private readonly scroll: HTMLElement;
  private readonly collapsed: HTMLButtonElement;
  private readonly kpiGrid: HTMLElement;
  private readonly tierRow: HTMLElement;
  private readonly deviceRow: HTMLElement;
  private readonly readout: HTMLElement;
  private readonly note: HTMLElement;
  private readonly badge: HTMLElement;

  private readonly filter: DockFilter = {
    tiers: new Set(ALL_TIER_LABELS),
    minDevices: 1,
  };
  private stats: ClusterStats | null = null;
  private isOpen = true;

  constructor(
    container: HTMLElement,
    private readonly callbacks: DockCallbacks,
  ) {
    this.kpiGrid = el('div', { class: 'kpi-grid' });
    this.tierRow = el('div', { class: 'chip-row' });
    this.deviceRow = el('div', { class: 'chip-row' });
    this.note = el('p', { class: 'provenance-note', hidden: 'hidden' });
    this.badge = el('span', { class: 'badge', text: '—' });
    this.readout = el('div', { class: 'dock-readout' }, [
      el('span', { text: 'Hover a marker for its reading · click to open the record' }),
    ]);

    const collapse = el('button', {
      class: 'link-button',
      type: 'button',
      text: 'Hide',
      'aria-expanded': 'true',
    });
    collapse.addEventListener('click', () => this.setOpen(false));

    const card = el('div', { class: 'dock-card' }, [
      el('div', { class: 'dock-card-header' }, [
        el('h2', { class: 'dock-title', text: 'In view' }),
        this.badge,
        collapse,
      ]),
      this.kpiGrid,
      el('div', { class: 'dock-divider' }),
      el('div', { class: 'dock-group' }, [
        el('h3', { class: 'dock-group-title', text: 'Severity tier' }),
        this.tierRow,
      ]),
      el('div', { class: 'dock-group' }, [
        el('h3', { class: 'dock-group-title', text: 'Corroboration' }),
        this.deviceRow,
      ]),
    ]);

    this.scroll = el('div', { class: 'dock-scroll' }, [this.note, card]);

    this.collapsed = el('button', {
      class: 'dock-collapsed',
      type: 'button',
      hidden: 'hidden',
      'aria-expanded': 'false',
    }) as HTMLButtonElement;
    this.collapsed.append(el('span', { text: 'Filters & totals' }));
    this.collapsed.addEventListener('click', () => this.setOpen(true));

    // The readout is the dock's last row and stays visible when collapsed — an
    // operator scrubbing the map still needs the reading under the cursor.
    this.root = el('aside', { class: 'dock', 'aria-label': 'Filters and totals' }, [
      this.scroll,
      this.collapsed,
      this.readout,
    ]);
    container.append(this.root);

    this.renderChips();
    this.renderKpis();
  }

  private setOpen(open: boolean): void {
    this.isOpen = open;
    this.scroll.hidden = !open;
    this.collapsed.hidden = open;
  }

  /** Fold in a fresh stats payload. */
  update(stats: ClusterStats): void {
    this.stats = stats;
    this.renderKpis();
    this.renderChips();
    this.renderBadge();
  }

  /** Called when the stats request fails — say so rather than showing stale numbers. */
  setUnavailable(): void {
    this.stats = null;
    this.renderKpis();
    this.renderBadge();
  }

  /**
   * Below the aggregate zoom the tiles carry no severity or device count, so the
   * chips genuinely cannot act. Say that rather than letting them look broken.
   */
  setFiltersApply(apply: boolean): void {
    this.note.hidden = apply;
    this.note.textContent = apply
      ? ''
      : 'Zoom in past level 13 to filter — grouped markers carry no severity or device count.';
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
      `${properties['distinct_devices'] ?? 0} devices`,
      `${properties['observation_count'] ?? 0} observations`,
    ].join(' · ');
  }

  private renderBadge(): void {
    if (!this.stats) {
      this.badge.textContent = '—';
      return;
    }
    const total = this.stats.open;
    const shown = this.shownCount();
    // Honest when filtered: "66 of 95" makes clear the map is not the whole story.
    this.badge.textContent =
      shown === total ? `${total} open defects` : `${shown} of ${total} open defects`;
  }

  /** Open clusters passing the tier filter, from SQL counts rather than the map. */
  private shownCount(): number {
    if (!this.stats) return 0;
    let total = 0;
    for (const [i, tier] of SEVERITY_TIERS.entries()) {
      if (this.filter.tiers.has(tier.label)) total += this.stats.tier_counts[i] ?? 0;
    }
    if (this.filter.tiers.has(UNRATED_LABEL)) total += this.stats.unrated;
    return total;
  }

  private renderKpis(): void {
    const s = this.stats;
    const severe = s ? (s.tier_counts[SEVERITY_TIERS.length - 1] ?? 0) : 0;
    const kpis: Array<[string, string]> = [
      [s ? String(s.open) : '—', 'Open defects in view'],
      // Percentages of a tiny denominator mislead, so below 10 open clusters the
      // share is withheld rather than rounded into confidence.
      [s && s.open >= 10 ? `${Math.round((severe / s.open) * 100)}%` : '—', 'Rated severe'],
      [s ? formatNumber(s.mean_confidence, 2) : '—', 'Mean confidence'],
      [s ? String(s.repaired_last_30d) : '—', 'Repaired this month'],
    ];
    this.kpiGrid.replaceChildren(
      ...kpis.map(([value, label]) =>
        el('div', { class: 'kpi' }, [
          el('span', { class: 'kpi-value', text: value }),
          el('span', { class: 'kpi-label', text: label }),
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
          if (this.filter.tiers.has(label)) this.filter.tiers.delete(label);
          else this.filter.tiers.add(label);
          this.emit();
        });
      }),
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
    chip.append(el('span', { text: count === null ? label : `${label} ${count}` }));
    chip.addEventListener('click', onClick);
    return chip;
  }

  private emit(): void {
    this.renderChips();
    this.renderBadge();
    this.callbacks.onFilterChange({
      tiers: new Set(this.filter.tiers),
      minDevices: this.filter.minDevices,
    });
  }

  destroy(): void {
    this.root.remove();
  }

  /** Exposed for the shell's initial paint. */
  get open(): boolean {
    return this.isOpen;
  }
}
