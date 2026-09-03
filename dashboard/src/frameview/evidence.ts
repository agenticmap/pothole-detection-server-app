/**
 * What the console says about one frame's detection evidence.
 *
 * Pure — no DOM, no fetch — so it can be tested without jsdom, and so the same
 * sentences appear on the panel thumbnail, in the frame viewer and in review rather
 * than being written three times and drifting.
 *
 * The problem this module exists to fix. Until now the panel rendered a single
 * number, `p = server_probability ?? device_probability`, which collapses three
 * different situations into one string:
 *
 *   - the server's detector scored this frame 0.62
 *   - the server never scored it and the phone said 0.62
 *   - a VLM overrode the detector and 0.62 is a BLEND of the two
 *
 * An operator triaging a repair cannot act on a number whose provenance is unknown,
 * and the third case is the one that matters most: under the hybrid backend
 * `server_probability` is `hybrid_v1._blend`'s output, not the detector's opinion.
 * `app/services/detection_boxes.py` says so directly — "an operator looking at 0.62
 * otherwise has no way to tell whether that is YOLO's opinion or a VLM override."
 */

import type { DetectionBox, VlmVerdict } from '../types.ts';
import type { BoxKind, OverlayBox } from '../review/overlay.ts';
import { detectorBoxLabel } from '../review/overlay.ts';

/**
 * The shape this module needs. Structural rather than a named response type, so a
 * `ClusterFrameItem` and a review `ReviewFrame` both satisfy it.
 */
export interface FrameEvidence {
  device_probability: number | null;
  server_probability: number | null;
  /** Absent on the review queue's shape; null means "never scored". */
  detected_at?: string | null;
  server_model_id?: string | null;
  server_boxes: DetectionBox[];
  device_boxes: DetectionBox[];
  vlm_verdict: VlmVerdict | null;
}

/** A probability as text. `—` only for a genuinely absent value. */
export function formatProb(p: number | null | undefined): string {
  // Deliberately not a truthiness check: 0.0 is the single commonest server score in
  // this corpus (2,183 of 5,615 frames) and means "the detector found nothing",
  // which is a measurement. Rendering it as `—` would erase the majority case.
  if (typeof p !== 'number' || !Number.isFinite(p)) return '—';
  return p.toFixed(2);
}

export interface ScoreLine {
  /** `srv` / `dev` — the prefix is what distinguishes them without colour. */
  label: string;
  value: string;
  title: string;
}

/**
 * Both scores, always both rows.
 *
 * Always both, even when one is null, because a missing row and a row reading `—`
 * say different things and only the second is honest: the panel used to omit the
 * on-device row entirely, so two frames with different provenance rendered
 * identically.
 */
export function scoreLines(frame: FrameEvidence): ScoreLine[] {
  return [
    {
      label: 'srv',
      value: formatProb(frame.server_probability),
      title: frame.vlm_verdict
        ? 'Server score — a BLEND of the detector and a VLM verdict, not the detector alone'
        : "The server detector's score for this frame",
    },
    {
      label: 'dev',
      value: formatProb(frame.device_probability),
      title: "The phone's own detector score, recorded at capture time",
    },
  ];
}

/**
 * "not yet scored", or null when it has been.
 *
 * Keys on `detected_at`, never on the probability. A frame the detector ran on and
 * found nothing in has `server_probability = 0.0` and IS scored; a frame nothing has
 * ever looked at has NULL and is not. Collapsing those is the same mistake
 * `boxed_at` exists to prevent one layer up.
 */
export function notScoredNote(frame: FrameEvidence): string | null {
  if (frame.detected_at === undefined) return null; // shape does not carry it
  return frame.detected_at === null ? 'not yet scored' : null;
}

/** Which detector box sets to draw. */
export interface BoxVisibility {
  server: boolean;
  device: boolean;
}

/** The detector boxes to hand to `FrameStage.draw`, labelled by source. */
export function overlayBoxesFor(frame: FrameEvidence, show: BoxVisibility): OverlayBox[] {
  const out: OverlayBox[] = [];
  const push = (boxes: DetectionBox[], kind: BoxKind, prefix: 'srv' | 'dev') => {
    for (const b of boxes) {
      out.push({
        x: b.x,
        y: b.y,
        w: b.w,
        h: b.h,
        kind,
        class_id: b.class_id,
        label: detectorBoxLabel(prefix, b),
      });
    }
  };
  if (show.server) push(frame.server_boxes, 'server', 'srv');
  if (show.device) push(frame.device_boxes, 'device', 'dev');
  return out;
}

export interface VlmSummary {
  /** `✓ VLM: pothole` — the glyph is there so the verdict is not carried by colour. */
  badge: string;
  confidence: string;
  severity: string;
  modelId: string;
  /** Verbatim. Truncation is CSS's job; the data layer must not silently shorten it. */
  rationale: string;
  /** Always present when a verdict exists. The most important line on the surface. */
  blendWarning: string;
}

export function vlmSummary(v: VlmVerdict | null): VlmSummary | null {
  if (!v) return null;
  return {
    badge: v.is_pothole ? '✓ VLM: pothole' : '✕ VLM: not a pothole',
    confidence: formatProb(v.confidence),
    // `severity` is passed straight through by parse_detection_boxes without a type
    // check, so it can arrive as a dict or a list. el({ text }) calls String() and
    // cannot inject, but "[object Object]" on screen is worse than an em dash.
    severity: typeof v.severity === 'string' && v.severity !== '' ? v.severity : '—',
    modelId: typeof v.model_id === 'string' && v.model_id !== '' ? v.model_id : '—',
    rationale: typeof v.rationale === 'string' ? v.rationale : '',
    blendWarning:
      'Server p is a blend of the detector and this VLM verdict, not the detector’s own score.',
  };
}
