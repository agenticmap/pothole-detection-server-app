/**
 * The cluster detail panel.
 *
 * Owns an AbortController per open. Clicking marker A then quickly marker B
 * would otherwise let A's detail request and its image fetches resolve after B
 * rendered, producing B's header with A's frames — and the "revoke on close"
 * cleanup never fires because the panel never closed. Every open aborts the
 * previous one.
 *
 * All server-supplied strings reach the DOM via textContent (see dom.ts). The
 * repair note is operator free text echoed back by the API, so innerHTML here
 * would be a stored-XSS path.
 */

import { ApiError, getCluster, setRepaired } from '../api.ts';
import { clear, el, field, formatDateTime, formatNumber, plural } from '../dom.ts';
import { severityLabel, tierFor } from '../severity.ts';
import type { ClusterDetailResponse } from '../types.ts';
import { loadFrames } from './frames.ts';

export interface PanelCallbacks {
  onClose: () => void;
  onRepairChanged: (clusterId: string, repaired: boolean) => void;
  canRepair: () => boolean;
}

export class DetailPanel {
  private readonly root: HTMLElement;
  private controller: AbortController | null = null;
  private current: ClusterDetailResponse | null = null;

  constructor(
    container: HTMLElement,
    private readonly callbacks: PanelCallbacks,
  ) {
    this.root = el('aside', {
      class: 'panel',
      hidden: 'hidden',
      'aria-label': 'Cluster detail',
    });
    container.append(this.root);
  }

  async open(clusterId: string): Promise<void> {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;

    this.root.hidden = false;
    this.renderSkeleton(clusterId);

    try {
      const detail = await getCluster(clusterId, controller.signal);
      if (controller.signal.aborted) return;
      this.current = detail;
      this.render(detail, controller.signal);
    } catch (err) {
      if (controller.signal.aborted) return;
      this.renderError(err instanceof Error ? err.message : 'Could not load this cluster.');
    }
  }

  close(): void {
    this.controller?.abort();
    this.controller = null;
    this.current = null;
    this.root.hidden = true;
    clear(this.root);
  }

  /**
   * Panel header: a small monospace cluster id above a display-font heading.
   *
   * The mockup's heading is a street name ("Spadina Ave · 195"). No street column
   * exists anywhere in the schema, so the heading carries the coordinates instead —
   * the most locating thing the record actually contains. Inventing a street name
   * for a screen a crew is dispatched from would be the wrong kind of fidelity.
   */
  private header(clusterId: string, place?: string): HTMLElement {
    const close = el('button', {
      class: 'icon-button panel-close',
      type: 'button',
      'aria-label': 'Close detail panel',
      text: '✕',
    });
    close.addEventListener('click', () => {
      this.close();
      this.callbacks.onClose();
    });
    return el('header', { class: 'panel-header' }, [
      el('div', { class: 'panel-heading-block' }, [
        el('span', { class: 'panel-cluster-id mono', text: clusterId }),
        place ? el('h2', { class: 'panel-title', text: place }) : null,
      ]),
      close,
    ]);
  }

  private renderSkeleton(clusterId: string): void {
    clear(this.root);
    this.root.append(
      this.header(clusterId),
      el('div', { class: 'panel-body' }, [
        el('div', { class: 'skeleton skeleton-line' }),
        el('div', { class: 'skeleton skeleton-line short' }),
        el('div', { class: 'skeleton skeleton-block' }),
      ]),
    );
  }

  private renderError(message: string): void {
    clear(this.root);
    this.root.append(
      this.header('Error'),
      el('div', { class: 'panel-body' }, [el('p', { class: 'error-text', text: message })]),
    );
  }

  private render(detail: ClusterDetailResponse, signal: AbortSignal): void {
    clear(this.root);

    const repaired = detail.repaired_at !== null;
    const tier = tierFor(detail.severity);

    const body = el('div', { class: 'panel-body' });

    // Status + severity, the two things an operator triages on.
    const badges = el('div', { class: 'badge-row' }, [
      el('span', {
        class: repaired ? 'badge badge-repaired' : 'badge badge-open',
        text: repaired ? 'Repaired' : 'Open',
      }),
      el('span', {
        class: 'badge badge-severity',
        // Colour is never the only channel: the label and the number carry it too.
        style: tier ? `background:var(${tier.varName})` : '',
        text: `${severityLabel(detail.severity)} · ${formatNumber(detail.severity, 2)}`,
      }),
    ]);
    body.append(badges);

    body.append(
      el('section', { class: 'panel-section' }, [
        field('Corroborating devices', plural(detail.distinct_devices, 'device')),
        field('Corroborating passes', plural(detail.distinct_passes ?? 0, 'pass', 'passes')),
        field('Observations', String(detail.observation_count)),
        field('Confidence', formatNumber(detail.confidence, 2)),
        field('Last seen', formatDateTime(detail.last_seen)),
        field('Source', detail.source ?? '—'),
        field('Location', `${detail.lat.toFixed(5)}, ${detail.lon.toFixed(5)}`, true),
      ]),
    );

    body.append(this.framesSection(detail, signal));
    body.append(this.membersSection(detail));
    if (detail.repair_history.length > 0) body.append(this.historySection(detail));

    const place = `${detail.lat.toFixed(5)}, ${detail.lon.toFixed(5)}`;
    this.root.append(this.header(detail.cluster_id, place), body, this.actionBar(detail, repaired));
  }

  private framesSection(detail: ClusterDetailResponse, signal: AbortSignal): HTMLElement {
    const section = el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: 'Camera frames' }),
    ]);

    if (detail.frames.length === 0) {
      section.append(
        el('p', {
          class: 'empty-note',
          text: 'No camera frames paired with this cluster. Sensor-only detection.',
        }),
      );
      return section;
    }

    const grid = el('div', { class: 'frame-grid' });
    const entries = detail.frames.map((frame) => {
      const img = el('img', {
        class: 'frame-thumb',
        alt: `Frame captured ${formatDateTime(frame.ts)}`,
        loading: 'lazy',
      });
      const score =
        frame.server_probability ?? frame.device_probability ?? null;
      const caption =
        frame.detected_at === null
          ? 'not yet scored'
          : `p=${formatNumber(score, 2)}`;
      grid.append(el('figure', { class: 'frame' }, [img, el('figcaption', { text: caption })]));
      return { img, frame };
    });
    section.append(grid);

    if (detail.frames_truncated) {
      section.append(el('p', { class: 'empty-note', text: 'More frames exist than shown.' }));
    }

    void loadFrames(entries, signal);
    return section;
  }

  private membersSection(detail: ClusterDetailResponse): HTMLElement {
    const section = el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: `Observations (${detail.members.length})` }),
    ]);

    const list = el('ul', { class: 'member-list' });
    for (const member of detail.members.slice(0, 12)) {
      list.append(
        el('li', { class: 'member' }, [
          el('span', { class: 'device-chip', text: member.device_ref }),
          el('span', { class: 'member-time', text: formatDateTime(member.ts) }),
          el('span', {
            class: 'member-score mono',
            text: formatNumber(member.sensor_p_pothole, 2),
          }),
        ]),
      );
    }
    section.append(list);
    if (detail.members.length > 12 || detail.members_truncated) {
      section.append(
        el('p', {
          class: 'empty-note',
          text: `Showing 12 of ${detail.observation_count} observations.`,
        }),
      );
    }
    return section;
  }

  private historySection(detail: ClusterDetailResponse): HTMLElement {
    const section = el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: 'Repair history' }),
    ]);
    const list = el('ul', { class: 'history-list' });
    for (const entry of detail.repair_history) {
      // Timeline layout: organic-shell.css makes .history-entry a
      // `9px 1fr` grid and draws the dot with .history-rail::before, so the rail
      // element is structural, not decoration — without it the row loses its
      // first column and the dot never appears.
      //
      // No data-actor is set: every row we can produce is an operator action
      // (repair_log's CHECK permits only repaired/unrepaired). The sage
      // [data-actor='system'] variant is styled and waiting if cluster-event
      // rows ever land.
      list.append(
        el('li', { class: 'history-entry' }, [
          el('span', { class: 'history-rail', 'aria-hidden': 'true' }),
          el('span', {
            class: 'history-action',
            text: entry.action === 'repaired' ? 'Marked repaired' : 'Reopened',
          }),
          el('span', { class: 'history-when', text: formatDateTime(entry.at) }),
          el('span', { class: 'history-who', text: entry.user_email ?? entry.user_id }),
          entry.note ? el('p', { class: 'history-note', text: entry.note }) : null,
        ]),
      );
    }
    section.append(list);
    return section;
  }

  private actionBar(detail: ClusterDetailResponse, repaired: boolean): HTMLElement {
    const bar = el('footer', { class: 'panel-actions' });

    // UI hint only — the server re-reads org_member on every write, so hiding
    // the control is convenience, not the access control.
    if (!this.callbacks.canRepair()) {
      bar.append(
        el('p', { class: 'empty-note', text: 'Marking repairs requires the staff role.' }),
      );
      return bar;
    }

    const note = el('input', {
      class: 'note-input',
      type: 'text',
      maxlength: '2000',
      placeholder: 'Optional note (e.g. crew, work order)',
      'aria-label': 'Repair note',
    });

    const button = el('button', {
      class: repaired ? 'button button-secondary' : 'button button-primary',
      type: 'button',
      text: repaired ? 'Reopen defect' : 'Mark repaired',
    });

    const status = el('p', { class: 'action-status', role: 'status' });

    button.addEventListener('click', async () => {
      button.disabled = true;
      status.textContent = repaired ? 'Reopening…' : 'Marking repaired…';
      try {
        const result = await setRepaired(detail.cluster_id, !repaired, note.value || null);
        this.callbacks.onRepairChanged(detail.cluster_id, !repaired);
        if (!result.changed) {
          status.textContent = 'Already in that state — nothing changed.';
        }
        await this.open(detail.cluster_id);
      } catch (err) {
        button.disabled = false;
        status.textContent =
          err instanceof ApiError && err.status === 403
            ? 'Your account no longer has permission to do that.'
            : err instanceof Error
              ? err.message
              : 'Could not update repair state.';
      }
    });

    bar.append(
      note,
      button,
      status,
      // Mockup line 297. Worth saying out loud: hiding the button for a viewer is
      // a convenience, and this tells an operator the real check is server-side.
      el('p', {
        class: 'action-footnote',
        text: 'Repair writes are re-checked against your role on the server.',
      }),
    );
    return bar;
  }

  get currentCluster(): ClusterDetailResponse | null {
    return this.current;
  }
}
