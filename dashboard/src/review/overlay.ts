/**
 * The box overlay: an SVG layer registered to a frame image.
 *
 * **Why SVG in a normalized viewBox rather than a canvas.** Boxes are stored
 * normalized 0..1, and the viewBox maps that range onto whatever size the image
 * happens to be rendered at. So a resize, a browser zoom, a DPR change, the detail
 * panel opening or the rail collapsing all need *zero* JavaScript — the browser
 * rescales the same coordinates. A canvas would need a full redraw on each, plus
 * manual hit-testing and manual DPR handling, and box counts here are single digits.
 *
 * Two details that are load-bearing rather than cosmetic:
 *
 *   * `preserveAspectRatio="none"` means the x and y scales differ, so a plain
 *     stroke renders thicker on one axis. `vector-effect="non-scaling-stroke"` is
 *     what keeps it even.
 *   * An integer viewBox (0..1000), not `0 0 1 1`. Sub-unit viewBoxes have a history
 *     of rounding oddly, and integers keep the emitted attribute values readable.
 *
 * Colours are inline `var()` references, never resolved hex. That is the house trick
 * (`panel.ts` does it for the severity badge) and it means a theme flip repaints the
 * overlay with no JS at all — unlike the map, which has to rebuild its whole style.
 */

import { el } from '../dom.ts';
import { dragToBox, hitTest, type NormBox, pointerToPx } from './geometry.ts';

const SVG_NS = 'http://www.w3.org/2000/svg';
/** The viewBox side. Normalized coordinates are multiplied by this. */
const SCALE = 1000;

/**
 * Who drew the box.
 *
 * `server` and `device` are split because they are different claims — the server's
 * detector and the phone's disagree on 1,006 frames in this corpus — and a console
 * that renders both as "the model" cannot show that. They are deliberately NOT
 * distinguished by hue: the five class colours belong to human boxes, and a detector
 * box must never look like a human's.
 */
export type BoxKind = 'human' | 'server' | 'device';

export interface OverlayBox {
  x: number;
  y: number;
  w: number;
  h: number;
  kind: BoxKind;
  /**
   * Class id, for the per-class hue. Optional and nullable because that is the
   * server's shape (`DetectionBox.class_id`); only human boxes are guaranteed one.
   */
  class_id?: number | null;
  label?: string | undefined;
  selected?: boolean;
}

/**
 * The class attribute for one box. Extracted from `draw()` so it can be tested.
 *
 * The rule it encodes was prose-only until now: a detector box carries NO
 * `review-box-c*` class, so it can never borrow a human class hue.
 */
export function boxClassName(
  kind: BoxKind,
  classId: number | null | undefined,
  selected: boolean,
): string {
  const parts = ['review-box', `review-box-${kind}`];
  if (kind === 'human' && typeof classId === 'number') parts.push(`review-box-c${classId}`);
  if (selected) parts.push('is-selected');
  return parts.join(' ');
}

function svg<K extends keyof SVGElementTagNameMap>(
  tag: K,
  attrs: Record<string, string | number>,
): SVGElementTagNameMap[K] {
  const node = document.createElementNS(SVG_NS, tag);
  for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, String(v));
  return node;
}

/**
 * The on-image label for a detector box: `srv pothole 0.62`, `dev 0.48`.
 *
 * The three-letter prefix is the channel that actually separates server from device.
 * The dash patterns in CSS are a mnemonic; the word is the answer, and it survives
 * greyscale, deuteranopia and a photograph of asphalt behind it.
 *
 * `confidence` is included because it has never been on screen anywhere in this
 * console — the map popup counts boxes, the panel showed one blended number, and
 * review showed the class name alone.
 */
export function detectorBoxLabel(
  prefix: 'srv' | 'dev',
  box: { label?: string | null; confidence?: number | null },
): string {
  const parts: string[] = [prefix];
  if (box.label) parts.push(box.label);
  // Guarded on `typeof`, not truthiness: a confidence of exactly 0 is a real
  // measurement and must print as 0.00 rather than vanish.
  if (typeof box.confidence === 'number' && Number.isFinite(box.confidence)) {
    parts.push(box.confidence.toFixed(2));
  }
  return parts.join(' ');
}

/**
 * A frame image with a box layer over it.
 *
 * The stage shrink-wraps the image (`inline-block`, `line-height: 0`) so the stage's
 * box and the image's rendered box are the same rectangle. **Never give the image
 * `object-fit`** — with `contain` the rendered content stops filling the element and
 * every percentage below would be off by the letterbox.
 */
export class FrameStage {
  readonly root: HTMLElement;
  readonly img: HTMLImageElement;
  private readonly layer: SVGSVGElement;
  /**
   * Class names, as HTML rather than SVG <text>.
   *
   * The overlay uses `preserveAspectRatio="none"`, so the x and y scales differ and
   * any <text> inside it is stretched. A sibling DOM layer positioned in percent
   * scales with the image for free and stays legible.
   */
  private readonly labels: HTMLElement;

  /**
   * `variant` appends a modifier class to the stage and the image, so a second
   * surface can size the stage differently without touching any of the rules above.
   * Everything load-bearing lives on the unmodified classes and keeps applying.
   */
  constructor(opts: { variant?: string } = {}) {
    const mod = opts.variant ? ` review-frame-${opts.variant}` : '';
    const stageMod = opts.variant ? ` review-stage-${opts.variant}` : '';
    this.img = el('img', { class: `review-frame${mod}`, alt: '' });
    this.layer = svg('svg', {
      class: 'review-overlay',
      viewBox: `0 0 ${SCALE} ${SCALE}`,
      preserveAspectRatio: 'none',
      'aria-hidden': 'true',
    });
    this.labels = el('div', { class: 'review-box-labels', 'aria-hidden': 'true' });
    this.root = el('div', { class: `review-stage${stageMod}` });
    this.root.append(this.img, this.layer, this.labels);
  }

  /** The image's rendered rectangle — the one coordinate space that matters. */
  rect(): DOMRect {
    return this.img.getBoundingClientRect();
  }

  /**
   * Give the stage the decoded image's own aspect ratio. **Call after every load.**
   *
   * This is what makes the no-`object-fit` contract hold rather than merely being
   * asserted: with the stage carrying the image's ratio, the stage box and the
   * rendered content box are identical by construction, so `width/height: 100%` on
   * the image distorts nothing and letterboxes nothing. It also lets the image scale
   * *up* — `max-width`/`max-height` alone only ever cap.
   *
   * It lived in ReviewModule, which meant the next surface to mount a frame would
   * have had to know to copy two lines or silently get every box coordinate wrong.
   */
  fit(): void {
    const { naturalWidth: w, naturalHeight: h } = this.img;
    if (w > 0 && h > 0) this.root.style.aspectRatio = `${w} / ${h}`;
  }

  setAlt(text: string): void {
    this.img.alt = text;
  }

  draw(boxes: readonly OverlayBox[]): void {
    this.layer.replaceChildren();
    this.labels.replaceChildren();
    for (const b of boxes) {
      // The selected box gets a white rect UNDERNEATH it rather than a recolour.
      // Recolouring would hide the one thing the box exists to say — its class —
      // and a mere thicker stroke is invisible against pale asphalt or sky. Two
      // rects is the only version that reads against all five hues and any photo.
      if (b.selected) {
        this.layer.append(
          svg('rect', {
            x: b.x * SCALE,
            y: b.y * SCALE,
            width: b.w * SCALE,
            height: b.h * SCALE,
            rx: 4,
            class: 'review-box-halo',
            'vector-effect': 'non-scaling-stroke',
          }),
        );
      }
      const rect = svg('rect', {
        x: b.x * SCALE,
        y: b.y * SCALE,
        width: b.w * SCALE,
        height: b.h * SCALE,
        rx: 4,
        // The class_id rides in the CSS class, not an inline colour: the five hues
        // are custom properties, so a theme flip repaints with no JS. Detector boxes
        // stay deliberately neutral — they must never look like a human's.
        class: boxClassName(b.kind, b.class_id, b.selected === true),
        'vector-effect': 'non-scaling-stroke',
      });
      this.layer.append(rect);

      // The label is what retires the colour-alone dependency: on the image the
      // stroke hue was previously the ONLY thing saying which class a box is, and
      // five hues is exactly where deuteranopia breaks down.
      if (b.label) {
        this.labels.append(
          el('span', {
            class: `review-box-label review-box-label-${b.kind}`,
            text: b.label,
            // Anchored to the box's top-left, nudged inside by CSS. Percent, so it
            // tracks the image through every resize with no JavaScript.
            style: `left:${b.x * 100}%;top:${b.y * 100}%`,
          }),
        );
      }
    }
  }

  clear(): void {
    this.layer.replaceChildren();
  }

  /**
   * Turn on drag-to-draw and click-to-select. Returns a teardown.
   *
   * The listeners for move and up live on `window`, not the stage: a drag that leaves
   * the image — which is exactly what boxing a defect at the frame edge looks like —
   * would otherwise never receive its mouseup and leave a ghost stuck on screen.
   */
  enableDrawing(opts: {
    isActive: () => boolean;
    boxesAt: () => readonly NormBox[];
    activeClass: () => number;
    onDraw: (box: NormBox) => void;
    onSelect: (index: number) => void;
  }): () => void {
    let start: { x: number; y: number } | null = null;
    let ghost: SVGRectElement | null = null;

    const at = (e: MouseEvent) => pointerToPx(e.clientX, e.clientY, this.rect());

    const down = (e: MouseEvent) => {
      if (!opts.isActive() || e.button !== 0) return;
      e.preventDefault();
      const rect = this.rect();
      const p = at(e);
      // A press on an existing box selects it. Selection first, so a mis-drag on top
      // of a box does not silently draw a second one over it.
      const hit = hitTest(opts.boxesAt(), p.x / rect.width, p.y / rect.height);
      opts.onSelect(hit);
      start = p;
      ghost = svg('rect', {
        class: 'review-box review-box-human is-selected',
        'vector-effect': 'non-scaling-stroke',
        rx: 4,
        x: 0,
        y: 0,
        width: 0,
        height: 0,
      });
      this.layer.append(ghost);
    };

    const move = (e: MouseEvent) => {
      if (!start || !ghost) return;
      const rect = this.rect();
      const p = at(e);
      const sx = (Math.min(start.x, p.x) / rect.width) * SCALE;
      const sy = (Math.min(start.y, p.y) / rect.height) * SCALE;
      ghost.setAttribute('x', String(sx));
      ghost.setAttribute('y', String(sy));
      ghost.setAttribute('width', String((Math.abs(p.x - start.x) / rect.width) * SCALE));
      ghost.setAttribute('height', String((Math.abs(p.y - start.y) / rect.height) * SCALE));
    };

    const up = (e: MouseEvent) => {
      if (!start) return;
      const rect = this.rect();
      const p = at(e);
      ghost?.remove();
      ghost = null;
      const from = start;
      start = null;
      // Returns null for a drag under the click threshold, so a stray click stays a
      // click rather than becoming a zero-area box the database would reject.
      const box = dragToBox(from, p, { width: rect.width, height: rect.height }, opts.activeClass());
      if (box) opts.onDraw(box);
    };

    this.root.addEventListener('mousedown', down);
    window.addEventListener('mousemove', move);
    window.addEventListener('mouseup', up);
    return () => {
      this.root.removeEventListener('mousedown', down);
      window.removeEventListener('mousemove', move);
      window.removeEventListener('mouseup', up);
    };
  }
}
