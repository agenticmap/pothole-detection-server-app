/**
 * The full-size frame viewer — this codebase's first `<dialog>`.
 *
 * Everything the detector produced about a frame has been on the wire for two phases
 * and on screen nowhere: the boxes, their per-box confidences, the on-device set, the
 * model id, the pairing deltas, and the VLM verdict that decides whether
 * `server_probability` is a detector score at all. A 185px thumbnail cannot answer
 * "is this really a pothole"; this can.
 *
 * **`showModal()`, never `show()`.** Not a style choice — it is what makes this
 * correct. The modal path puts the dialog in the TOP LAYER, so it renders above
 * MapLibre's canvas and above `.panel-container` regardless of stacking context; it
 * makes the rest of the document `inert`, which IS the focus trap, implemented by the
 * browser rather than by a hand-rolled Tab cycle; and it gives `::backdrop` and
 * Escape-to-close for free. It is also why `--z-modal` stays unused: the top layer
 * supersedes z-index entirely.
 *
 * **The `<dialog>` display trap** is the `[hidden]` trap wearing a different hat. The
 * UA stylesheet is `dialog { display: none }` and only `dialog[open]` displays, so
 * `.frame-dialog { display: grid }` would win on specificity and render the dialog
 * WHILE CLOSED. Every display rule for this component is scoped to
 * `.frame-dialog[open]`.
 *
 * The viewer holds COPIES of what it is opened with, never a reference back to the
 * panel: `panel.open()` re-enters itself after a repair and aborts its controller on
 * every re-open, which would otherwise tear this content out from under the operator.
 */

import { getFrameObjectUrl } from '../api.ts';
import { clear, el, formatDateTime, formatNumber } from '../dom.ts';
import { FrameStage } from '../review/overlay.ts';
import type { FrameDetail } from '../types.ts';
import {
  type BoxVisibility,
  notScoredNote,
  overlayBoxesFor,
  scoreLines,
  vlmSummary,
} from './evidence.ts';

export interface OpenOptions {
  /**
   * The list to page through. Copied, not held by reference.
   *
   * Typed as FrameDetail, the WIDER of the two shapes: its
   * `paired_observation_id` is nullable, so a ClusterFrameItem (which always has
   * one) is assignable here but not the reverse. That is what lets the map open an
   * unpaired frame through the same viewer the panel uses.
   */
  frames: readonly FrameDetail[];
  index: number;
  /** Focus returns here on close, if it is still in the document. */
  trigger: HTMLElement | null;
}

let dialogSeq = 0;

export class FrameViewer {
  private readonly dialog: HTMLDialogElement;
  private readonly titleId = `frame-dialog-title-${++dialogSeq}`;
  private readonly stage = new FrameStage({ variant: 'viewer' });
  private readonly title = el('h2', { class: 'frame-dialog-title mono', id: this.titleId });
  private readonly rail = el('div', { class: 'frame-dialog-rail' });
  private readonly footer = el('footer', { class: 'frame-dialog-footer' });

  private frames: FrameDetail[] = [];
  private index = 0;
  private trigger: HTMLElement | null = null;
  private controller: AbortController | null = null;
  private objectUrl: string | null = null;
  /** Both on by default: an operator triaging a repair wants the evidence. */
  private visible: BoxVisibility = { server: true, device: true };

  constructor(private readonly onClosed?: () => void) {
    this.dialog = document.createElement('dialog');
    this.dialog.className = 'frame-dialog';
    this.dialog.setAttribute('aria-labelledby', this.titleId);

    const close = el('button', {
      class: 'icon-button',
      type: 'button',
      'aria-label': 'Close frame viewer',
      text: '✕',
    });
    close.addEventListener('click', () => this.dialog.close());

    this.dialog.append(
      el('div', { class: 'frame-dialog-inner' }, [
        el('header', { class: 'frame-dialog-header' }, [this.title, close]),
        el('div', { class: 'frame-dialog-body' }, [
          el('div', { class: 'frame-dialog-stage' }, [this.stage.root]),
          this.rail,
        ]),
        this.footer,
      ]),
    );

    // Arrow keys page the list. Bound on the dialog, not the document, so it cannot
    // fire once closed — and the document is inert while it is open anyway.
    this.dialog.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowRight') {
        e.preventDefault();
        this.go(1);
      } else if (e.key === 'ArrowLeft') {
        e.preventDefault();
        this.go(-1);
      }
    });

    // `close` fires for Escape, the button and a programmatic close alike, so the
    // teardown lives here once rather than at three call sites.
    this.dialog.addEventListener('close', () => {
      this.controller?.abort();
      this.controller = null;
      this.revoke();
      this.stage.clear();
      this.stage.resetRotation();
      this.stage.img.removeAttribute('src');
      // The spec restores focus itself, but the panel re-renders on several paths and
      // the trigger may no longer be connected — in which case focus lands on <body>.
      // Checked rather than assumed.
      if (this.trigger?.isConnected) this.trigger.focus();
      this.trigger = null;
      this.onClosed?.();
    });

    document.body.append(this.dialog);
  }

  private revoke(): void {
    if (this.objectUrl) {
      URL.revokeObjectURL(this.objectUrl);
      this.objectUrl = null;
    }
  }

  private current(): FrameDetail | null {
    return this.frames[this.index] ?? null;
  }

  open(opts: OpenOptions): void {
    if (opts.frames.length === 0) return;
    this.frames = [...opts.frames];
    this.index = Math.min(Math.max(opts.index, 0), this.frames.length - 1);
    this.trigger = opts.trigger;
    this.dialog.showModal();
    this.render();
    void this.loadImage();
  }

  close(): void {
    if (this.dialog.open) this.dialog.close();
  }

  destroy(): void {
    this.close();
    this.dialog.remove();
  }

  private go(step: number): void {
    const next = this.index + step;
    if (next < 0 || next >= this.frames.length) return;
    this.index = next;
    // A turn belongs to the frame it was applied to, not to the list.
    this.stage.resetRotation();
    this.render();
    void this.loadImage();
  }

  private render(): void {
    const frame = this.current();
    if (!frame) return;
    this.title.textContent = frame.client_id;
    this.stage.setAlt(`Frame captured ${formatDateTime(frame.ts)}`);
    this.renderRail(frame);
    this.renderFooter();
    this.drawBoxes();
  }

  private drawBoxes(): void {
    const frame = this.current();
    if (!frame) return this.stage.clear();
    this.stage.draw(overlayBoxesFor(frame, this.visible));
  }

  private boxToggle(label: string, count: number, key: keyof BoxVisibility): HTMLElement {
    // The count rides in the label so an empty set reads as "the detector found
    // nothing" rather than "the toggle is broken".
    const chip = el('button', {
      class: 'chip',
      type: 'button',
      'aria-pressed': String(this.visible[key]),
      text: `${label} (${count})`,
    });
    if (count === 0) chip.setAttribute('disabled', '');
    chip.addEventListener('click', () => {
      this.visible = { ...this.visible, [key]: !this.visible[key] };
      chip.setAttribute('aria-pressed', String(this.visible[key]));
      this.drawBoxes();
    });
    return chip;
  }

  private renderRail(frame: FrameDetail): void {
    clear(this.rail);
    // A turn is for looking at a frame that arrived sideways. View-only and reset on
    // every open -- 20 legacy frames were corrected at rest by
    // scripts/fix_frame_orientation.py, and a persisted rotation here would become a
    // second answer to "which way is up", which is what that script eliminated.
    const turn = el('button', {
      class: 'chip',
      type: 'button',
      'aria-pressed': String(this.stage.rotation() !== 0),
      text: 'Turn 90°',
      title: 'Rotate the view for a frame that was stored sideways. Not saved.',
    });
    turn.addEventListener('click', () => {
      const deg = this.stage.rotate();
      turn.setAttribute('aria-pressed', String(deg !== 0));
      turn.textContent = deg === 0 ? 'Turn 90°' : `Turned ${deg}°`;
      this.drawBoxes();
    });

    this.rail.append(
      el('div', { class: 'chip-row' }, [
        this.boxToggle('Server boxes', frame.server_boxes.length, 'server'),
        this.boxToggle('On-device boxes', frame.device_boxes.length, 'device'),
        turn,
      ]),
      strokeLegend(),
      this.detectionSection(frame),
      this.pairingSection(frame),
    );
    const vlm = this.vlmSection(frame);
    if (vlm) this.rail.append(vlm);
  }

  private detectionSection(frame: FrameDetail): HTMLElement {
    const section = el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: 'Detection' }),
    ]);
    const notScored = notScoredNote(frame);
    if (notScored) section.append(el('p', { class: 'empty-note', text: notScored }));

    const dl = el('dl', { class: 'frame-dialog-facts' });
    for (const line of scoreLines(frame)) {
      dl.append(
        el('dt', { text: line.label === 'srv' ? 'Server p' : 'On-device p', title: line.title }),
        el('dd', { class: 'mono', text: line.value }),
      );
    }
    dl.append(
      el('dt', { text: 'Model' }),
      el('dd', { class: 'mono', text: frame.server_model_id ?? '—' }),
      el('dt', { text: 'Scored at' }),
      el('dd', { text: formatDateTime(frame.detected_at) }),
      el('dt', { text: 'Captured' }),
      el('dd', { text: formatDateTime(frame.ts) }),
    );
    section.append(dl);
    return section;
  }

  /** The pairing story: on the wire since Phase 2.2d and never once rendered. */
  private pairingSection(frame: FrameDetail): HTMLElement {
    return el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: 'Pairing' }),
      el('dl', { class: 'frame-dialog-facts' }, [
        el('dt', { text: 'Fused confidence' }),
        el('dd', { class: 'mono', text: formatNumber(frame.fused_confidence, 2) }),
        el('dt', { text: 'Δt to the impact' }),
        el('dd', { class: 'mono', text: frame.delta_ms === null ? '—' : `${frame.delta_ms} ms` }),
        el('dt', { text: 'Δd to the impact' }),
        el('dd', {
          class: 'mono',
          text: frame.delta_m === null ? '—' : `${frame.delta_m.toFixed(1)} m`,
        }),
      ]),
    ]);
  }

  private vlmSection(frame: FrameDetail): HTMLElement | null {
    const vlm = vlmSummary(frame.vlm_verdict);
    if (!vlm) return null;
    return el('section', { class: 'panel-section' }, [
      el('h3', { class: 'panel-section-title', text: 'VLM verification' }),
      el('p', { class: 'badge frame-vlm-verdict', text: vlm.badge }),
      // The most important line here, and it is nowhere else in the product: with a
      // verdict present, Server p above is hybrid_v1._blend's output, not the
      // detector's opinion.
      el('p', { class: 'review-status is-warn', text: `⚠  ${vlm.blendWarning}` }),
      el('dl', { class: 'frame-dialog-facts' }, [
        el('dt', { text: 'VLM confidence' }),
        el('dd', { class: 'mono', text: vlm.confidence }),
        el('dt', { text: 'Severity' }),
        el('dd', { text: vlm.severity }),
        el('dt', { text: 'Model' }),
        el('dd', { class: 'mono', text: vlm.modelId }),
      ]),
      // textContent via el({ text }); dom.ts has no innerHTML escape hatch, and this
      // is third-party model output echoed to a browser.
      vlm.rationale ? el('blockquote', { class: 'vlm-rationale', text: vlm.rationale }) : null,
      el('p', {
        class: 'empty-note',
        text: 'A language model’s account of the image, not a measurement.',
      }),
    ]);
  }

  private renderFooter(): void {
    clear(this.footer);
    if (this.frames.length <= 1) return;
    const prev = el('button', { class: 'button button-secondary', type: 'button', text: '← Previous' });
    const next = el('button', { class: 'button button-secondary', type: 'button', text: 'Next →' });
    if (this.index === 0) prev.setAttribute('disabled', '');
    if (this.index === this.frames.length - 1) next.setAttribute('disabled', '');
    prev.addEventListener('click', () => this.go(-1));
    next.addEventListener('click', () => this.go(1));
    this.footer.append(
      prev,
      el('span', {
        class: 'frame-dialog-counter',
        text: `${this.index + 1} of ${this.frames.length}`,
      }),
      next,
    );
  }

  private async loadImage(): Promise<void> {
    const frame = this.current();
    if (!frame) return;
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;
    const wanted = frame.client_id;

    try {
      const url = await getFrameObjectUrl(frame.image_url, controller.signal);
      if (controller.signal.aborted || this.current()?.client_id !== wanted) {
        URL.revokeObjectURL(url);
        return;
      }
      // Revoke the previous url only once the new one is in hand, so paging never
      // flashes an empty stage between frames.
      this.revoke();
      this.objectUrl = url;
      this.stage.img.src = url;
      await this.stage.img.decode().catch(() => {});
      if (this.current()?.client_id !== wanted) return;
      this.stage.fit();
      this.drawBoxes();
    } catch {
      if (controller.signal.aborted) return;
      this.stage.img.removeAttribute('src');
      this.stage.clear();
      this.stage.setAlt('Image unavailable');
    }
  }
}

/**
 * Three line swatches naming the three box kinds.
 *
 * SVG lines rather than coloured dots, because what separates the kinds IS the stroke
 * pattern and width. A dot would claim they differ by colour, which they deliberately
 * do not — server and device share a hue on purpose.
 */
function strokeLegend(): HTMLElement {
  const SVG = 'http://www.w3.org/2000/svg';
  const row = el('div', { class: 'frame-legend' });
  const kinds: [string, string][] = [
    ['review-box-human', 'Human'],
    ['review-box-server', 'Server detector'],
    ['review-box-device', 'On-device detector'],
  ];
  for (const [cls, label] of kinds) {
    const svg = document.createElementNS(SVG, 'svg');
    svg.setAttribute('class', 'frame-legend-swatch');
    svg.setAttribute('viewBox', '0 0 24 8');
    svg.setAttribute('aria-hidden', 'true');
    const line = document.createElementNS(SVG, 'line');
    for (const [k, v] of Object.entries({ x1: 1, y1: 4, x2: 23, y2: 4 })) {
      line.setAttribute(k, String(v));
    }
    line.setAttribute('class', `review-box ${cls}`);
    line.setAttribute('vector-effect', 'non-scaling-stroke');
    svg.append(line);
    row.append(el('span', { class: 'frame-legend-item' }, [svg, el('span', { text: label })]));
  }
  return row;
}
