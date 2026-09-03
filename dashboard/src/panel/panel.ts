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
import { notScoredNote, overlayBoxesFor, scoreLines } from '../frameview/evidence.ts';
import { FrameViewer } from '../frameview/viewer.ts';
import { FrameStage } from '../review/overlay.ts';
import type { ClusterDetailResponse } from '../types.ts';
import { spanNoteText } from './corroboration.ts';
import { type FrameEntry, loadFrames } from './frames.ts';

/** The corroboration warning, or nothing when the cluster is genuinely corroborated. */
function spanNote(passes: number, spanS: number | null): HTMLElement | null {
  const text = spanNoteText(passes, spanS);
  return text === null ? null : el('p', { class: 'empty-note', text });
}

export interface PanelCallbacks {
  onClose: () => void;
  onRepairChanged: (clusterId: string, repaired: boolean) => void;
  canRepair: () => boolean;
}

export class DetailPanel {
  private readonly root: HTMLElement;
  private controller: AbortController | null = null;
  private current: ClusterDetailResponse | null = null;
  /**
   * One dialog for the panel's lifetime, opened with copies of the frame list.
   *
   * Built once rather than per open because a `<dialog>` in the top layer is cheap to
   * keep and expensive to get wrong: creating one per click would leak an element on
   * every thumbnail press.
   */
  private readonly viewer = new FrameViewer();

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
    // Closed with the panel: the viewer's content came from this panel's detail, so
    // leaving it open over a closed panel would show frames for a cluster the
    // operator has already dismissed.
    this.viewer.close();
    this.root.hidden = true;
    clear(this.root);
  }

  /** Release the dialog. Sign-out and session expiry both route through here. */
  destroy(): void {
    this.close();
    this.viewer.destroy();
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
        field('Corroborating passes', plural(detail.distinct_passes, 'pass', 'passes')),
        field('Observations', String(detail.observation_count)),
        field('Confidence', formatNumber(detail.confidence, 2)),
        field('Last seen', formatDateTime(detail.last_seen)),
        field('Source', detail.source ?? '—'),
        field('Location', `${detail.lat.toFixed(5)}, ${detail.lon.toFixed(5)}`, true),
        // member_span_s rendered as a judgement rather than a float. migrations/015
        // calls it "the diagnostic that exposed the problem in the first place":
        // a cluster whose members span seconds is one drive-past, and reporting
        // "1 pass" without that context reads like a measurement rather than a
        // warning that nothing has corroborated this defect yet.
        spanNote(detail.distinct_passes, detail.member_span_s),
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

    // One toggle for the whole grid rather than one per thumbnail. Unlike review,
    // detector boxes are ON by default here: review hides them because showing a
    // labeller where the model looked before they judge is the anchoring Phase 2.7b
    // measured making the detector monotonically worse. A panel operator is triaging
    // a repair, not producing ground truth, so that reasoning does not transfer —
    // for them the boxes are the evidence.
    let showBoxes = true;
    const redraws: (() => void)[] = [];
    const toggle = el('button', {
      class: 'chip',
      type: 'button',
      'aria-pressed': 'true',
      text: 'Detector boxes',
    });
    toggle.addEventListener('click', () => {
      showBoxes = !showBoxes;
      toggle.setAttribute('aria-pressed', String(showBoxes));
      for (const redraw of redraws) redraw();
    });
    section.append(el('div', { class: 'chip-row' }, [toggle]));

    const grid = el('div', { class: 'frame-grid' });
    const entries: FrameEntry[] = detail.frames.map((frame) => {
      const stage = new FrameStage({ variant: 'thumb' });
      stage.setAlt(`Frame captured ${formatDateTime(frame.ts)}`);

      const redraw = () =>
        stage.draw(overlayBoxesFor(frame, { server: showBoxes, device: showBoxes }));
      redraws.push(redraw);

      // A cell of fixed ratio holds a stage that shrink-wraps the image. The old rule
      // was `aspect-ratio: 4/3; object-fit: cover` over a PORTRAIT 480x640 corpus, so
      // every thumbnail kept the middle ~56% of its height and threw away the top and
      // bottom quarters — and on a forward-facing capture the road surface is in the
      // bottom. It was invisible only because nothing drew boxes to expose it.
      const cell = el('div', { class: 'frame-cell' }, [stage.root]);

      // A real <button>, so the viewer is reachable by keyboard and announced as
      // activatable. Styled back to nothing (.frame-open) — it must not look like a
      // button, but it must behave like one.
      const open = el('button', {
        class: 'frame-open',
        type: 'button',
        'aria-label': `Open frame captured ${formatDateTime(frame.ts)} at full size`,
      }, [cell]);
      open.addEventListener('click', () => {
        // The whole list, so the viewer can page through it. _FRAMES_SQL orders by
        // fused_confidence DESC, so the order the operator sees is the order the
        // pipeline ranked them.
        this.viewer.open({ frames: detail.frames, index: detail.frames.indexOf(frame), trigger: open });
      });

      const notScored = notScoredNote(frame);
      const caption = el('figcaption', { class: 'frame-scores' });
      if (notScored) {
        caption.append(el('span', { class: 'frame-score-note', text: notScored }));
      }
      for (const line of scoreLines(frame)) {
        caption.append(
          el('span', { class: 'frame-score', title: line.title }, [
            el('span', { class: 'frame-score-label', text: line.label }),
            el('span', { class: 'frame-score-value mono', text: line.value }),
          ]),
        );
      }
      if (frame.vlm_verdict) {
        // Presence only. The verdict itself needs room the caption does not have —
        // but an operator must be able to tell, without a click, that this frame's
        // server score is a blend rather than the detector's own number.
        caption.append(
          el('span', {
            class: 'badge frame-vlm-badge',
            text: 'VLM',
            title: 'A VLM verified this frame — the server score is a blend.',
          }),
        );
      }

      grid.append(el('figure', { class: 'frame' }, [open, caption]));
      return { stage, frame, onReady: redraw };
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
