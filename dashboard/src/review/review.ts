/**
 * The frame review module.
 *
 * Why it exists: the detector's binding constraint is a shortage of in-domain
 * positives, and 1,041 unlabelled frames sit at score >= 0.30 — the densest expected
 * pothole yield in the corpus. `scripts/label_frames.py` can label them, but only as
 * a terminal session on one machine. This makes it a staff task.
 *
 * Lifecycle mirrors the detail panel's: the module owns its root, renders into it,
 * and holds one AbortController per queue load, aborting the previous. It is
 * constructed lazily on first entry and `destroy()`ed only on sign-out or session
 * expiry — switching modules hides it, so a half-finished pass survives a trip to
 * the map.
 *
 * All server-supplied strings reach the DOM through `el({ text })`, i.e. textContent.
 * `note` and the VLM `rationale` are both free text echoed back by the API, so
 * innerHTML here would be a stored-XSS path — and `dom.ts` deliberately offers no
 * escape hatch.
 */

import { ApiError } from '../api.ts';
import { clear, el, formatNumber } from '../dom.ts';
import type { ReviewUrlState } from '../shell.ts';
import {
  fetchQueue,
  postBoxes,
  postSubmit,
  type QueueParams,
  type QueueResponse,
  postVerdict,
} from './api.ts';
import { FrameImageCache } from './images.ts';
import { boxBindings, dispatch, REASON_TAGS, verdictBindings } from './keys.ts';
import { isThin, type NormBox } from './geometry.ts';
import { detectorBoxLabel, FrameStage, type OverlayBox } from './overlay.ts';
import {
  advance,
  applySubmit,
  type Entry,
  needsSave,
  progressLine,
  resolveJump,
  resolveLanding,
  resolveMove,
  stateOf,
  submittableIds,
  toEntry,
} from './queue-state.ts';

export interface ReviewCallbacks {
  onStateChange: (state: ReviewUrlState) => void;
  /** UI hint only — the server re-reads org_member on every write. */
  canWrite: () => boolean;
}

/** The bands from phase-2.9's measured table, so the UI and the analysis agree. */
const BANDS: { label: string; min: number | null; max: number | null }[] = [
  { label: 'All', min: null, max: null },
  { label: '0.00–0.05', min: 0, max: 0.05 },
  { label: '0.05–0.15', min: 0.05, max: 0.15 },
  { label: '0.15–0.30', min: 0.15, max: 0.3 },
  { label: '0.30–0.40', min: 0.3, max: 0.4 },
  { label: '0.40–0.75', min: 0.4, max: 0.75 },
  { label: '≥ 0.30', min: 0.3, max: null },
];

/** Remembers whether the key legend is expanded. */
const LEGEND_KEY = 'roadwatch.review.keys';

const DEFAULT_PARAMS: QueueParams = {
  mode: 'verdict',
  order: 'score',
  review: false,
  minScore: 0.3, // the seam this module exists to clear
  maxScore: null,
  includeModelBoxes: false,
  seed: null,
  limit: 50,
};

export class ReviewModule {
  private readonly root: HTMLElement;
  private readonly images = new FrameImageCache();
  private readonly stage = new FrameStage();
  private controller: AbortController | null = null;
  private readonly onKeyDown: (e: KeyboardEvent) => void;

  private params: QueueParams = { ...DEFAULT_PARAMS };
  private entries: Entry[] = [];
  private cursor = 0;
  private meta: QueueResponse | null = null;
  private note = '';
  private showModelBoxes = false;
  /**
   * The status channel. A level, not just a string.
   *
   * It used to be one field rendered as `.error-text` with `role="alert"`, so a
   * successful submit appeared in danger red and was announced as an alert, and the
   * thin-box notice -- explicitly "a warning, never a refusal" -- read as an error.
   */
  private status: { level: 'ok' | 'warn' | 'error'; text: string } = { level: 'ok', text: '' };
  /**
   * Created ONCE and mutated, never re-appended.
   *
   * render() clears the subtree on every keystroke, so appending a fresh
   * role="alert" node each time made assistive tech re-announce the same error on
   * every subsequent `j`. panel.ts:291 already does it this way.
   */
  private readonly liveRegion = el('p', {
    class: 'review-live',
    role: 'status',
    'aria-live': 'polite',
  });
  private sessionCount = 0;
  private activeClass = 0;
  private selected = -1;
  private stopDrawing: (() => void) | null = null;
  /**
   * A single FIFO chain for navigation, so there is never more than one box write
   * in flight. Out-of-order landing then is not merely unlikely, it is
   * unrepresentable. The cap stops a held key stacking a backlog.
   */
  private navChain: Promise<void> = Promise.resolve();
  private navQueued = 0;
  /** A frame id from the URL hash, honoured once on the next load. */
  private pendingFrame: string | null = null;

  constructor(
    container: HTMLElement,
    private readonly callbacks: ReviewCallbacks,
  ) {
    this.root = el('section', { class: 'review', 'aria-label': 'Frame review' });
    container.append(this.root);

    // The first keyboard handler in this codebase. Bound on the document because the
    // operator's hands never leave the keys, and removed in destroy().
    this.onKeyDown = (e) => {
      if (this.root.hidden) return;
      // A modal <dialog> makes the rest of the document inert, so a CLICK cannot
      // reach the page behind it -- but `keydown` listeners bound on the document
      // still fire, and this is one. Without this guard, `j` pressed while the frame
      // viewer is open advances the queue underneath it and `1` records a verdict on
      // a frame the operator is examining through a modal: ground truth the promotion
      // gate is judged on, written by a keystroke aimed at something else.
      //
      // The check lives here rather than in keys.ts::dispatch so that module stays
      // pure and testable without jsdom -- its guards are the ones that stop a held
      // key writing labels, and they are worth being able to test. Anything on the
      // page, not just our own viewer, correctly suppresses the shortcuts.
      if (document.querySelector('dialog[open]')) return;
      dispatch(this.bindings(), e);
    };
    document.addEventListener('keydown', this.onKeyDown);

    // Boxes are positioned against the image's RENDERED size, so anything that
    // changes it has to trigger a repaint. A ResizeObserver on the image catches the
    // panel opening and the rail collapsing, neither of which fires a window resize.
    new ResizeObserver(() => this.drawBoxes()).observe(this.stage.img);

    this.stopDrawing = this.stage.enableDrawing({
      isActive: () => this.isBox() && this.callbacks.canWrite(),
      boxesAt: () => this.current()?.boxes ?? [],
      activeClass: () => this.activeClass,
      onDraw: (box) => this.addBox(box),
      onSelect: (index) => {
        this.selected = index;
        this.drawBoxes();
      },
    });
  }

  // ── URL state ───────────────────────────────────────────────────────────────

  applyUrlState(state: ReviewUrlState | null): void {
    if (!state) return;
    this.params = {
      ...this.params,
      mode: state.mode,
      order: state.order,
      review: state.review,
      minScore: state.minScore,
      maxScore: state.maxScore,
      seed: state.seed,
    };
    // A deep link is a ONE-SHOT landing, not a filter. Held until the next load and
    // cleared unconditionally there -- keeping it set would yank the operator back to
    // this frame on every `r`, which is a filter pretending to be a link.
    this.pendingFrame = state.frame;
  }

  urlState(): ReviewUrlState {
    return {
      mode: this.params.mode,
      order: this.params.order,
      review: this.params.review,
      minScore: this.params.minScore,
      maxScore: this.params.maxScore,
      seed: this.meta?.seed ?? this.params.seed,
      frame: this.current()?.item.client_id ?? null,
    };
  }

  // ── Lifecycle ───────────────────────────────────────────────────────────────

  async show(): Promise<void> {
    this.root.hidden = false;
    if (this.meta === null) await this.load();
  }

  hide(): void {
    this.root.hidden = true;
  }

  destroy(): void {
    this.controller?.abort();
    this.controller = null;
    document.removeEventListener('keydown', this.onKeyDown);
    this.stopDrawing?.();
    this.stopDrawing = null;
    this.images.clear();
    this.root.remove();
  }

  // ── Data ────────────────────────────────────────────────────────────────────

  private current(): Entry | null {
    // noUncheckedIndexedAccess makes every index access `T | undefined`. Funnelled
    // through one accessor so the narrowing happens once rather than everywhere.
    return this.entries[this.cursor] ?? null;
  }

  async load(): Promise<void> {
    this.controller?.abort();
    const controller = new AbortController();
    this.controller = controller;

    // Captured HERE, not in render(): a reload clears the subtree at skeleton time,
    // long before render() runs, so a capture inside render() would only ever see
    // <body>. Reloads are reachable from the keyboard (`r`, the band chips, the
    // pass controls, and `b` when the model boxes have to be fetched).
    const focus = this.captureFocus();
    this.renderSkeleton();
    try {
      const res = await fetchQueue(this.params, controller.signal);
      if (controller.signal.aborted) return;
      this.meta = res;
      this.entries = res.items.map(toEntry);
      const landing = resolveLanding(
        this.entries.map((e) => e.item.client_id),
        this.pendingFrame,
      );
      this.pendingFrame = null;
      this.cursor = landing.cursor;
      if (landing.missed) {
        this.setStatus(
          'warn',
          'That frame is not in this queue — showing the first one. Widen the band or clear it.',
        );
      }
      this.note = '';
      this.render();
      this.restoreFocus(focus);
      this.callbacks.onStateChange(this.urlState());
    } catch (err) {
      if (controller.signal.aborted) return;
      this.renderError(
        err instanceof ApiError && err.status === 403
          ? 'Your account does not have access to the review queue.'
          : err instanceof Error
            ? err.message
            : 'Could not load the review queue.',
      );
    }
  }

  private setBand(min: number | null, max: number | null): void {
    this.params = { ...this.params, minScore: min, maxScore: max };
    void this.load();
  }

  // ── Actions ─────────────────────────────────────────────────────────────────

  private bindings() {
    if (this.isBox()) {
      return boxBindings(
        {
          setClass: (i) => {
            this.activeClass = i;
            this.render();
          },
          save: () => this.boxMove(1, false),
          submit: () => void this.submit(),
          reload: () => void this.load(),
          deleteSelected: () => this.deleteSelected(),
          deselect: () => {
            this.selected = -1;
            this.render();
          },
          toggleModelBoxes: () => this.toggleModelBoxes(),
          move: (step, shift) => this.boxMove(step, shift),
          jump: (to) => this.boxJump(to),
        },
        this.meta?.classes ?? [],
      );
    }
    return verdictBindings({
      judge: (label) => void this.judge(label),
      tag: (text) => {
        // Toggling: pressing the same tag twice clears it, so a mis-key is one
        // keystroke to undo rather than a trip to the textarea.
        this.note = this.note === text ? '' : text;
        this.render();
      },
      focusNote: () => this.root.querySelector<HTMLTextAreaElement>('.review-note')?.focus(),
      toggleModelBoxes: () => this.toggleModelBoxes(),
      move: (step, shift) => this.move(step, shift),
      reload: () => void this.load(),
    });
  }

  // ── Box mode ────────────────────────────────────────────────────────────────

  private isBox(): boolean {
    return this.params.mode === 'box';
  }

  private setMode(mode: 'verdict' | 'box'): void {
    if (mode === this.params.mode) return;
    this.params = { ...this.params, mode };
    this.selected = -1;
    // Refetch: the two modes run different SQL predicates, so the queue is a
    // different population, not a different view of the same one.
    void this.load();
  }

  /**
   * Save the current frame's boxes as a draft.
   *
   * Returns false when the write did not land, and the caller must then NOT
   * navigate — leaving a frame whose boxes were not stored is the one failure an
   * operator cannot discover later.
   */
  private async commit(): Promise<boolean> {
    const entry = this.current();
    if (!entry) return true;
    if (entry.imageFailed) return true; // never record work on a frame nobody saw
    if (!needsSave(entry)) return true; // the boxKey short-circuit
    if (!this.callbacks.canWrite()) {
      this.setStatus('error', 'Your account cannot save boxes.');
      return false;
    }

    try {
      const res = await postBoxes(entry.item.client_id, entry.boxes);
      entry.item.human_boxes = res.boxes.map((b) => ({ ...b }));
      entry.item.boxes_drafted_at = new Date().toISOString();
      entry.dirty = false;
      entry.peeked = false;
      entry.writeError = null;
      if (res.thin_warnings.length) {
        this.setStatus(
          'warn',
          `Saved. ${res.thin_warnings.join('/')}: box REGIONS, not LINES — a sliver around a ` +
            `hairline crack is mostly undamaged asphalt, and the model learns that the asphalt ` +
            `IS the class.`,
        );
      } else {
        this.setStatus('ok', 'Boxes saved.');
      }
      return true;
    } catch (err) {
      const status = err instanceof ApiError ? err.status : 0;
      entry.writeError =
        status === 409
          ? 'This frame has no verdict yet — judge it before boxing it.'
          : err instanceof Error
            ? err.message
            : 'Could not save those boxes.';
      this.setStatus('error', entry.writeError);
      return false;
    }
  }

  /** Queue a navigation. One chain, so a fast `j j j` saves in order or not at all. */
  private queueMove(fn: () => Promise<void>): void {
    if (this.navQueued >= 4) return;
    this.navQueued++;
    this.navChain = this.navChain
      .then(fn)
      .catch(() => {
        this.navQueued = 0;
      })
      .finally(() => {
        this.navQueued--;
      });
  }

  private boxMove(step: number, shift: boolean): void {
    this.queueMove(async () => {
      const r = resolveMove(this.cursor, this.entries.length, 'box', step, shift);
      // The save is attempted even when the move is a no-op. At the last frame the
      // MOVE does nothing; the SAVE still must happen. Not doing that is the
      // recorded CLI bug where the final frame "never became a draft, so Submit had
      // no id to sign off" and it never left the queue.
      if (r.save && !(await this.commit())) {
        this.render();
        return;
      }
      this.cursor = r.cursor;
      this.selected = -1;
      // Cleared on every move. It used to persist for the rest of the session, so
      // "this frame's image did not load" stayed on screen fifty frames later.
      this.setStatus('ok', '');
      if (r.peek) {
        const e = this.current();
        if (e) e.peeked = true;
      }
      this.render();
      this.callbacks.onStateChange(this.urlState());
    });
  }

  private boxJump(to: 'start' | 'end'): void {
    this.queueMove(async () => {
      // A jump crosses frames it never displayed, so it records none of them.
      const r = resolveJump(to, this.entries.length, 'box');
      this.cursor = r.cursor;
      this.selected = -1;
      const e = this.current();
      if (e && r.peek) e.peeked = true;
      this.render();
    });
  }

  private addBox(box: NormBox): void {
    const entry = this.current();
    if (!entry || !this.isBox()) return;
    entry.boxes.push(box);
    entry.dirty = true;
    entry.peeked = false; // drawing on it means you looked at it
    this.selected = entry.boxes.length - 1;
    this.render();
  }

  private deleteSelected(): void {
    const entry = this.current();
    if (!entry || this.selected < 0) return;
    entry.boxes.splice(this.selected, 1);
    entry.dirty = true;
    entry.peeked = false;
    this.selected = -1;
    this.render();
  }

  private async submit(): Promise<void> {
    const entry = this.current();
    // "I pressed submit while looking at this frame" should include this frame --
    // not doing so is how the last one in a queue used to get stranded. The peeked
    // frame is the one exception: it was glanced at, not worked on.
    if (entry && !entry.peeked && !(await this.commit())) {
      this.render();
      return;
    }

    const ids = submittableIds(this.entries);
    // Counted BEFORE the call, because the server never sees these -- they are
    // excluded client-side, so its response has no category for them. Without this
    // an operator who Shift-navigated through fifteen frames, came back and boxed
    // them, saw a number quietly smaller than expected and no reason why.
    const peekedOut = this.entries.filter(
      (e) => e.peeked && e.item.boxes_drafted_at !== null && e.item.boxed_at === null,
    ).length;
    const blindOut = this.entries.filter(
      (e) => e.imageFailed && e.item.boxes_drafted_at !== null,
    ).length;

    if (ids.length === 0) {
      this.setStatus(
        'warn',
        peekedOut
          ? `Nothing to submit. ${peekedOut} frame(s) are marked peeked — revisit them without Shift to include them.`
          : 'Nothing to submit.',
      );
      this.render();
      return;
    }

    try {
      const out = await postSubmit(ids);
      applySubmit(this.entries, ids, out);

      const parts = [`Signed off ${out.finalized}.`];
      if (out.already_finalized) parts.push(`${out.already_finalized} already were.`);
      // Reported, never swallowed: a frame refused here is one the operator thinks
      // is done and the exporter will never see.
      if (out.skipped_unjudged.length) {
        parts.push(`${out.skipped_unjudged.length} had no verdict.`);
      }
      if (out.skipped_undrafted.length) {
        parts.push(`${out.skipped_undrafted.length} had no boxes saved.`);
      }
      if (peekedOut) {
        parts.push(`${peekedOut} skipped as peeked — revisit without Shift to include them.`);
      }
      if (blindOut) {
        parts.push(`${blindOut} skipped: image never loaded.`);
      }
      const anySkipped =
        out.skipped_unjudged.length || out.skipped_undrafted.length || peekedOut || blindOut;
      this.setStatus(anySkipped ? 'warn' : 'ok', parts.join(' '));
      this.announce(parts.join(' '));

      if (!this.params.review) {
        // Finished work should not be in the way of the work that is left. Stay on
        // the same frame if it survived, else land on whatever took its place.
        const here = this.current()?.item.client_id ?? null;
        this.entries = this.entries.filter((e) => e.item.boxed_at === null);
        const at = this.entries.findIndex((e) => e.item.client_id === here);
        this.cursor = at >= 0 ? at : Math.min(this.cursor, Math.max(this.entries.length - 1, 0));
      }
      this.render();
      this.callbacks.onStateChange(this.urlState());
    } catch (err) {
      this.setStatus(
        'error',
        err instanceof Error ? err.message : 'Submit failed — nothing was signed off.',
      );
      this.render();
    }
  }

  /**
   * Keep the keyboard's place across a re-render.
   *
   * render() rebuilds the whole subtree, so without this anything focused is
   * destroyed and focus falls to <body> -- meaning a Tab user restarts from the top
   * of the document on every single keystroke. It matters more since the `blur()`
   * workarounds were removed: those used to hide the problem by always throwing
   * focus away, so it was never visible as focus *loss*.
   *
   * Keyed on a stable data attribute rather than an index or a DOM path, because the
   * rebuilt tree is a different set of nodes and the list it came from may be a
   * different length.
   */
  private captureFocus(): { key: string; caret: number | null } | null {
    const active = document.activeElement as HTMLElement | null;
    if (!active || !this.root.contains(active)) return null;
    const key = active.dataset['focusKey'];
    if (!key) return null;
    const caret =
      active instanceof HTMLTextAreaElement || active instanceof HTMLInputElement
        ? active.selectionStart
        : null;
    return { key, caret };
  }

  private restoreFocus(saved: { key: string; caret: number | null } | null): void {
    if (!saved) return;
    const next = this.root.querySelector<HTMLElement>(`[data-focus-key="${CSS.escape(saved.key)}"]`);
    if (!next) return;
    next.focus();
    if (
      saved.caret !== null &&
      (next instanceof HTMLTextAreaElement || next instanceof HTMLInputElement)
    ) {
      next.setSelectionRange(saved.caret, saved.caret);
    }
  }

  private setStatus(level: 'ok' | 'warn' | 'error', text: string): void {
    this.status = { level, text };
  }

  /**
   * Say what just happened, for a screen reader and for the eye.
   *
   * Pressing `1` used to change only the photograph and an 11px counter, so there
   * was no way to confirm a keystroke did what was meant without navigating back.
   * Mutating one persistent live region is what makes it audible.
   */
  private announce(text: string): void {
    this.liveRegion.textContent = text;
  }

  private toggleModelBoxes(): void {
    this.showModelBoxes = !this.showModelBoxes;
    if (this.showModelBoxes && !this.params.includeModelBoxes && !this.meta?.blind) {
      // The boxes are not in the payload: `include_model_boxes` is a server-side
      // gate, not a render flag, so revealing them costs a refetch.
      this.params = { ...this.params, includeModelBoxes: true };
      void this.load();
      return;
    }
    this.render();
  }

  private move(step: number, shift: boolean): void {
    const r = resolveMove(this.cursor, this.entries.length, this.params.mode, step, shift);
    this.cursor = r.cursor;
    this.setStatus('ok', '');
    if (r.peek) {
      const e = this.current();
      if (e) e.peeked = true;
    }
    this.render();
    this.callbacks.onStateChange(this.urlState());
  }

  private async judge(label: number): Promise<void> {
    const entry = this.current();
    if (!entry) return;
    if (!this.callbacks.canWrite()) {
      this.setStatus('error', 'Your account cannot record verdicts.');
      this.render();
      return;
    }
    if (entry.imageFailed) {
      // You must not judge a frame you cannot see.
      this.setStatus('error', 'This frame’s image did not load — it cannot be judged.');
      this.render();
      return;
    }

    const note = this.note || null;
    // Optimistic: the cursor advances immediately because the rhythm is the point.
    // A failure raises a banner naming the frame rather than yanking the view back.
    entry.item.label = label;
    entry.item.note = note;
    entry.peeked = false;
    this.sessionCount++;
    this.note = ''; // a reason belongs to one frame, never to the next
    this.cursor = advance(this.cursor, this.entries.length);
    const name = label === 1 ? 'Pothole' : label === 0 ? 'Not a pothole' : 'Unsure';
    const at = Math.min(this.cursor + 1, this.entries.length);
    this.announce(`${name} recorded. Frame ${at} of ${this.entries.length}.`);
    this.render();
    this.callbacks.onStateChange(this.urlState());

    try {
      await postVerdict(entry.item.client_id, label, note);
      entry.writeError = null;
    } catch (err) {
      const message =
        err instanceof ApiError && err.status === 403
          ? 'Your account no longer has permission to do that.'
          : err instanceof Error
            ? err.message
            : 'Could not record that verdict.';
      entry.writeError = message;
      this.setStatus('error', `${entry.item.client_id.slice(0, 8)}… — ${message}`);
      this.render();
    }
  }

  // ── Render ──────────────────────────────────────────────────────────────────

  private renderSkeleton(): void {
    clear(this.root);
    this.root.append(
      el('div', { class: 'review-body' }, [
        el('div', { class: 'skeleton skeleton-line' }),
        el('div', { class: 'skeleton skeleton-block' }),
      ]),
    );
  }

  private renderError(message: string): void {
    clear(this.root);
    // The rail survives. panel.ts keeps its header and close button on error for the
    // same reason: an error view with no controls is a dead end, and the controls are
    // exactly what the operator needs to get out of it -- widen the band, change the
    // pass, reload.
    const rail = el('div', { class: 'review-rail' }, [
      this.renderBands(),
      this.renderPassControls(),
    ]);
    this.root.append(
      el('div', { class: 'review-workspace' }, [
        el('div', { class: 'review-main' }, [
          el('p', { class: 'review-status is-error', role: 'alert', text: `✕  ${message}` }),
        ]),
        rail,
      ]),
    );
  }

  private drawBoxes(): void {
    const entry = this.current();
    if (!entry) return this.stage.clear();
    const classes = this.meta?.classes ?? [];
    const boxes: OverlayBox[] = [];
    if (this.showModelBoxes) {
      // Both detector sets, and they are labelled apart. The server's model and the
      // phone's disagree on 1,006 frames in this corpus, and until now the console
      // drew only the server's — so a labeller had no way to see the disagreement
      // they were, in effect, being asked to adjudicate.
      for (const b of entry.item.server_boxes) {
        boxes.push({ ...b, kind: 'server', label: detectorBoxLabel('srv', b) });
      }
      for (const b of entry.item.device_boxes) {
        boxes.push({ ...b, kind: 'device', label: detectorBoxLabel('dev', b) });
      }
    }
    entry.boxes.forEach((b, i) => {
      boxes.push({
        ...b,
        kind: 'human',
        selected: i === this.selected,
        label: classes[b.class_id] ?? `class ${b.class_id}`,
      });
    });
    this.stage.draw(boxes);
  }

  private render(): void {
    const meta = this.meta;
    if (!meta) return;
    const focus = this.captureFocus();
    clear(this.root);

    const entry = this.current();

    // Two columns: the photograph is the work surface and gets the pane, the
    // controls live in a rail that never scrolls away.
    //
    // The single-column version wasted 73% of the pane and pushed the verdict
    // buttons off the bottom, because `max-height: 68vh` on a PORTRAIT corpus caps
    // the image at ~466px wide however wide the screen is, while the chrome above
    // it ate 191px. Both symptoms, one cause.
    const stageCol = el('div', { class: 'review-main' }, [this.renderTopline(meta)]);
    // Bands live in the rail, not above the image. They are a session-level filter
    // changed maybe once an hour, and sitting above the work surface they cost ~55px
    // of image height on every single frame.
    const rail = el('div', { class: 'review-rail' }, [this.renderBands(), this.renderPassControls()]);

    if (this.entries.length === 0) {
      stageCol.append(
        el('p', {
          class: 'empty-note',
          text: this.params.review
            ? 'No finished frames to review yet.'
            : `Nothing left in this band. ${meta.counts.outstanding.toLocaleString()} outstanding overall — widen the band or clear it.`,
        }),
      );
    } else if (!entry) {
      // The cursor deliberately runs one past the end in verdict mode: that is how
      // "this page is done" is expressed rather than sticking on the last frame.
      stageCol.append(
        el('p', {
          class: 'empty-note',
          text: `All ${this.entries.length} frames on this page are done. Press r to load the next batch.`,
        }),
      );
    } else {
      stageCol.append(this.renderFrame(entry));

      if (this.isBox()) {
        rail.append(this.renderClassPicker(), this.renderBoxList(entry), this.renderBoxActions());
        if (entry.item.boxed_at !== null) {
          // The server permits re-boxing a signed-off frame and leaves boxed_at set,
          // so this silently replaces boxes the exporter may already have shipped.
          // Announced, not discovered.
          rail.append(
            el('p', {
              class: 'review-status is-warn',
              role: 'status',
              text: '!  Already signed off — saving replaces boxes the exporter may already have used.',
            }),
          );
        }
        if (!this.callbacks.canWrite()) {
          // Box mode used to give a read-only user a silent no-op: the drag simply
          // did nothing, forever, with no notice. Verdict mode always had one.
          rail.append(
            el('p', {
              class: 'empty-note',
              text: 'Read-only: drawing boxes needs the staff role. The server re-checks on every write.',
            }),
          );
        }
      } else {
        rail.append(this.renderControls(entry));
      }
    }

    if (this.status.text) {
      // Only a genuine failure is an alert. A success or an advisory routed through
      // role="alert" trains the operator to ignore the channel that reports failure.
      rail.append(
        el('p', {
          class:
            this.status.level === 'error'
              ? 'review-status is-error'
              : this.status.level === 'warn'
                ? 'review-status is-warn'
                : 'review-status is-ok',
          // The glyph is what keeps severity off colour alone.
          text: `${this.status.level === 'error' ? '✕' : this.status.level === 'warn' ? '!' : '✓'}  ${this.status.text}`,
          ...(this.status.level === 'error' ? { role: 'alert' } : {}),
        }),
      );
    }
    // Appended, never rebuilt -- the same node for the life of the module.
    rail.append(this.liveRegion);
    rail.append(this.renderLegend());

    this.root.append(el('div', { class: 'review-workspace' }, [stageCol, rail]));

    // Drives the crosshair cursor from state rather than a second boolean in CSS.
    this.stage.root.dataset['drawing'] = this.isBox() && this.callbacks.canWrite() ? '1' : '0';
    if (entry) {
      // Drawn synchronously as well as after decode. mountImage has to wait for the
      // image so boxes are never sized against the PREVIOUS frame, but when the
      // frame has not changed that wait made a delete linger on screen for a frame.
      this.drawBoxes();
      void this.mountImage(entry);
    }
    this.restoreFocus(focus);
  }

  /**
   * One compact row instead of four stacked blocks.
   *
   * The old header spent 191px of vertical budget on a title the operator already
   * knows (the rail says "Frame review"), three separate lines of counts, and a
   * mode toggle — which is what pushed the verdict buttons past the bottom of the
   * viewport. Same information, one line, mode toggle pulled to the right where it
   * reads as a control rather than another filter.
   */
  private renderTopline(meta: QueueResponse): HTMLElement {
    const position =
      this.entries.length === 0
        ? '—'
        : `${Math.min(this.cursor + 1, this.entries.length)} / ${this.entries.length}`;

    const counts = el('div', { class: 'review-counts' }, [
      // Kept as a heading for structure; visually it is the small eyebrow.
      el('h2', { class: 'review-title', text: 'Frame review' }),
      el('span', {
        class: 'review-progress',
        text: progressLine({
          drafts: this.entries.filter((e) => stateOf(e) === 'draft').length,
          queueLength: this.entries.length,
          reviewMode: this.params.review,
          mode: this.params.mode,
        }),
      }),
      el('span', { class: 'review-meta-sep', text: '·', 'aria-hidden': 'true' }),
      el('span', { class: 'review-progress', text: `frame ${position}` }),
      el('span', { class: 'review-meta-sep', text: '·', 'aria-hidden': 'true' }),
      // Reported verbatim with the server's own snapshot rather than decremented
      // locally: `counts` is a band-global figure, and in box mode it does not move
      // as you draft. Faking freshness is what dock.ts refuses to do for KPI deltas.
      el('span', {
        class: 'review-progress',
        text: `${meta.counts.outstanding.toLocaleString()} outstanding in band`,
      }),
    ]);

    if (this.sessionCount) {
      counts.append(
        el('span', { class: 'review-meta-sep', text: '·', 'aria-hidden': 'true' }),
        el('span', { class: 'review-session', text: `${this.sessionCount} this session` }),
      );
    }

    return el('div', { class: 'review-topline' }, [counts, this.renderModeToggle()]);
  }

  private renderModeToggle(): HTMLElement {
    const row = el('div', { class: 'chip-row' });
    for (const [mode, label] of [
      ['verdict', 'Judge'],
      ['box', 'Draw boxes'],
    ] as const) {
      const chip = el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': String(this.params.mode === mode),
        text: label,
        'data-focus-key': `mode-${mode}`,
      });
      chip.addEventListener('click', () => {
        this.setMode(mode);
      });
      row.append(chip);
    }
    return row;
  }

  /**
   * The class picker. Chips carry the toggle semantics; the swatch carries identity.
   *
   * The swatch is an inline style holding a `var()`, not a resolved hex — the same
   * trick panel.ts uses for the severity badge — so a theme flip repaints it with no
   * JS. The legend (shell.ts) resolves to hex instead and has to re-render itself;
   * this is the better of the two patterns.
   */
  private renderClassPicker(): HTMLElement {
    const row = el('div', { class: 'chip-row' });
    (this.meta?.classes ?? []).forEach((name, i) => {
      const chip = el('button', {
        class: 'chip review-class-chip',
        type: 'button',
        'aria-pressed': String(this.activeClass === i),
        'data-focus-key': `class-${i}`,
      });
      chip.append(
        el('span', {
          class: 'review-swatch',
          style: `background:var(--review-class-${i})`,
          'aria-hidden': 'true',
        }),
        el('span', { text: name }),
        el('kbd', { class: 'mono', text: String(i + 1) }),
      );
      chip.addEventListener('click', () => {
        this.activeClass = i;
        this.render();
      });
      row.append(chip);
    });
    return el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Draw as' }),
      row,
    ]);
  }

  /**
   * The per-frame box list.
   *
   * Exists so a box hidden behind a larger one is still reachable and countable
   * without hunting on the image, and so the thin-box warning can point at a row.
   */
  private renderBoxList(entry: Entry): HTMLElement {
    const classes = this.meta?.classes ?? [];
    const ratio = this.meta?.thin_aspect_ratio ?? 6;
    const regions = new Set(this.meta?.region_classes ?? []);
    const group = el('div', { class: 'dock-group' }, [
      el('h3', {
        class: 'dock-group-title',
        text: `Boxes on this frame (${entry.boxes.length})`,
      }),
    ]);

    if (entry.boxes.length === 0) {
      group.append(
        el('p', {
          class: 'empty-note',
          // Saving nothing is a real answer, and the operator should know it counts.
          text: 'None. Saving zero boxes records "reviewed, genuinely clean".',
        }),
      );
      return group;
    }

    const list = el('ul', { class: 'review-box-list' });
    entry.boxes.forEach((b, i) => {
      const name = classes[b.class_id] ?? `class ${b.class_id}`;
      const thin = regions.has(name) && isThin(b.w, b.h, ratio);
      const row = el('li', {
        class: i === this.selected ? 'review-box-row is-selected' : 'review-box-row',
      });
      const remove = el('button', {
        class: 'icon-button',
        type: 'button',
        text: '×',
        'aria-label': `Delete ${name} box`,
        'data-focus-key': `boxdel-${i}`,
      });
      remove.addEventListener('click', () => {
        this.selected = i;
        this.deleteSelected();
      });
      row.append(
        el('span', {
          class: 'review-swatch',
          style: `background:var(--review-class-${b.class_id})`,
          'aria-hidden': 'true',
        }),
        el('span', { text: name }),
      );
      if (thin) row.append(el('span', { class: 'review-thin-flag', text: 'thin' }));
      row.append(remove);
      row.addEventListener('click', () => {
        this.selected = i;
        this.render();
      });
      list.append(row);
    });
    group.append(list);
    return group;
  }

  private renderBoxActions(): HTMLElement {
    const drafts = this.entries.filter((e) => stateOf(e) === 'draft').length;
    const submit = el('button', {
      class: 'button button-primary',
      type: 'button',
      text: `Submit ${drafts} draft${drafts === 1 ? '' : 's'} (s)`,
      disabled: drafts === 0,
      'data-focus-key': 'submit',
    });
    submit.addEventListener('click', () => {
      void this.submit();
    });
    return el('div', { class: 'dock-group' }, [
      submit,
      // Box mode's counts do not move as you draft -- the server's predicate keys on
      // boxed_at, and saving sets boxes_drafted_at. Only submitting advances it.
      el('p', {
        class: 'empty-note',
        text: 'Reloading re-syncs this page; only submitting clears frames from it.',
      }),
    ]);
  }

  private renderBands(): HTMLElement {
    const row = el('div', { class: 'chip-row' });
    for (const b of BANDS) {
      const active = this.params.minScore === b.min && this.params.maxScore === b.max;
      const chip = el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': String(active),
        text: b.label,
        'data-focus-key': `band-${b.label}`,
      });
      chip.addEventListener('click', () => {
        this.setBand(b.min, b.max);
      });
      row.append(chip);
    }
    return el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Score band' }),
      row,
    ]);
  }

  /**
   * How this pass runs: the ordering, and whether it is a check-my-work pass.
   *
   * Both were parsed from the URL hash and honoured by the queue from the start, but
   * had no control anywhere -- so a blind pass or a review pass could only be entered
   * by hand-editing the address bar, and the "No finished frames to review yet" empty
   * state was for a mode nothing could reach.
   */
  private renderPassControls(): HTMLElement {
    const orders = el('div', { class: 'chip-row' });
    for (const [order, label, why] of [
      ['score', 'Score order', 'Highest-scoring frames first — the densest seam.'],
      [
        'blind',
        'Blind',
        "The server withholds the model's score and boxes entirely, so its opinion cannot anchor yours.",
      ],
    ] as const) {
      const chip = el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': String(this.params.order === order),
        text: label,
        title: why,
        'data-focus-key': `order-${order}`,
      });
      chip.addEventListener('click', () => {
        if (this.params.order === order) return;
        // Seed is reset: it reproduces an ordering over a population, and switching
        // order changes the population.
        this.params = { ...this.params, order, seed: null };
        void this.load();
      });
      orders.append(chip);
    }

    const reviewBox = el('input', {
      type: 'checkbox',
      class: 'dock-toggle-input',
      'data-focus-key': 'review-mode',
    }) as HTMLInputElement;
    reviewBox.checked = this.params.review;
    reviewBox.addEventListener('change', () => {
      this.params = { ...this.params, review: reviewBox.checked };
      void this.load();
    });

    return el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Pass' }),
      orders,
      // A checkbox, not another chip: styles.css records the console's own rule that
      // chips are filters over one set, and this switches to a different set.
      el('label', { class: 'dock-toggle' }, [
        reviewBox,
        el('span', { text: 'Check my work' }),
      ]),
      el('p', {
        class: 'dock-group-hint',
        text: 'Check-my-work queues ONLY finished frames. It is not an include-everything switch — mixing the two means paging past completed work.',
      }),
    ]);
  }

  private renderFrame(entry: Entry): HTMLElement {
    const p = entry.item.server_probability;
    const caption = this.meta?.blind
      ? 'score withheld — blind pass'
      : p === null
        ? 'not yet scored'
        : `p = ${formatNumber(p, 3)}`;

    const meta = el('div', { class: 'review-frame-meta' }, [
      el('span', { class: 'mono', text: entry.item.client_id }),
      el('span', { text: caption }),
    ]);

    // Both of these were tracked in the state machine and rendered nowhere, so a
    // frame whose write FAILED looked identical to one that saved, and a frame
    // excluded from submit gave no clue why.
    if (entry.writeError) {
      meta.append(el('span', { class: 'review-flag is-error', text: `not saved — ${entry.writeError}` }));
    }
    if (entry.peeked) {
      meta.append(
        el('span', {
          class: 'review-flag is-warn',
          text: 'peeked — will not be submitted',
          title: 'You arrived here with Shift or Home/End, so this frame counts as looked-at, not worked-on.',
        }),
      );
    }
    return el('div', { class: 'review-frame-wrap' }, [
      el('div', { class: 'review-stage-wrap' }, [this.stage.root]),
      meta,
    ]);
  }

  private renderControls(entry: Entry): HTMLElement {
    const recorded = entry.item.label;
    const verdicts = el('div', { class: 'chip-row' });
    for (const [label, text] of [
      [1, 'Pothole (1)'],
      [0, 'Not a pothole (0)'],
      [-1, 'Unsure (u)'],
    ] as const) {
      const chip = el('button', {
        class: 'chip',
        type: 'button',
        'aria-pressed': String(recorded === label),
        text,
        'data-focus-key': `verdict-${label}`,
      });
      chip.addEventListener('click', () => {
        void this.judge(label);
      });
      verdicts.append(chip);
    }

    const tags = el('div', { class: 'chip-row' });
    for (const [k, text] of Object.entries(REASON_TAGS)) {
      const chip = el('button', {
        class: 'chip is-secondary',
        type: 'button',
        'aria-pressed': String(this.note === text),
        text: `${text} (${k})`,
        'data-focus-key': `reason-${k}`,
      });
      chip.addEventListener('click', () => {
        this.note = this.note === text ? '' : text;
        this.render();
      });
      tags.append(chip);
    }

    const note = el('textarea', {
      class: 'input review-note',
      rows: 2,
      placeholder: 'Why? (n to focus)',
      maxlength: 200,
      'data-focus-key': 'note',
    }) as HTMLTextAreaElement;
    note.value = this.note;
    note.addEventListener('input', () => {
      this.note = note.value;
    });
    // The textarea owns these two keys locally, so no verdict can be recorded while
    // the operator is typing a reason.
    note.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        note.blur();
      } else if (e.key === 'Escape') {
        this.note = '';
        note.value = '';
        note.blur();
      }
    });

    const group = el('div', { class: 'dock-group' }, [
      el('h3', { class: 'dock-group-title', text: 'Verdict' }),
      verdicts,
      el('h3', { class: 'dock-group-title', text: 'Reason' }),
      tags,
      note,
    ]);

    if (!this.callbacks.canWrite()) {
      group.append(
        el('p', {
          class: 'empty-note',
          text: 'Read-only: recording a verdict needs the staff role.',
        }),
      );
    }
    return group;
  }

  /**
   * The key legend, collapsed by default after the first session.
   *
   * Generated from the same array the dispatcher reads, so it cannot describe a key
   * that does not exist or miss one that does. Collapsible because it is essential
   * on day one and permanent furniture by frame 300; the preference is remembered
   * so the operator states it once. `<details>` gives the disclosure semantics,
   * keyboard operation and screen-reader announcement for free.
   */
  private renderLegend(): HTMLElement {
    const list = el('dl', { class: 'review-legend' });
    for (const b of this.bindings()) {
      list.append(
        el('dt', { class: 'mono', text: b.keyLabel }),
        el('dd', { text: b.description }),
      );
    }

    const details = el('details', { class: 'review-keys' }) as HTMLDetailsElement;
    details.open = this.legendOpen();
    details.append(el('summary', { class: 'dock-group-title', text: 'Keys' }), list);
    details.addEventListener('toggle', () => {
      try {
        localStorage.setItem(LEGEND_KEY, details.open ? '1' : '0');
      } catch {
        // Private mode or a blocked store; the legend just stops being remembered.
      }
    });
    return details;
  }

  /** Open on a first visit, then whatever the operator last chose. */
  private legendOpen(): boolean {
    try {
      return (localStorage.getItem(LEGEND_KEY) ?? '1') === '1';
    } catch {
      return true;
    }
  }

  private async mountImage(entry: Entry): Promise<void> {
    this.images.pin(entry.item.client_id);
    this.images.syncWindow(
      this.entries.map((e) => e.item.client_id),
      this.cursor,
    );
    this.stage.setAlt(`Frame ${entry.item.client_id}`);
    try {
      const url = await this.images.get(entry.item.client_id, entry.item.image_url, 'now');
      if (this.current() !== entry) return; // moved on while it loaded
      this.stage.img.src = url;
      await this.stage.img.decode().catch(() => {});
      // The stage takes the frame's own ratio so it can grow to fill the pane while
      // its box stays identical to the image's -- which is what keeps the overlay
      // exact without object-fit. Set per frame: this corpus mixes portrait and
      // rotated-landscape captures. Lives on the stage so the next surface to mount
      // a frame gets the contract by construction rather than by remembering.
      this.stage.fit();
      // Drawn only after decode: boxes positioned against the previous image's
      // rendered size would be silently wrong.
      this.drawBoxes();
      this.images.prefetch(
        this.entries.map((e) => e.item),
        this.cursor,
      );
    } catch {
      if (this.current() !== entry) return;
      entry.imageFailed = true;
      this.stage.img.removeAttribute('src');
      this.stage.clear();
      this.stage.setAlt('Image unavailable');
    }
  }
}
