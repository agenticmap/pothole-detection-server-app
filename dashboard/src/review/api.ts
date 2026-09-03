/**
 * The review endpoints. Thin wrappers over `request` from ../api.ts, which attaches
 * the bearer and retries once on a 401 — the same shape `stats.ts` uses.
 *
 * Types here are hand-written mirrors of `app/models/review.py`. Keep field names
 * exact, as `../types.ts` says of its own mirrors.
 */

import { request } from '../api.ts';
import type { NormBox } from './geometry.ts';
import type { ReviewFrame } from './queue-state.ts';

export type QueueMode = 'verdict' | 'box';
export type QueueOrder = 'score' | 'blind';

export interface QueueParams {
  mode: QueueMode;
  order: QueueOrder;
  review: boolean;
  minScore: number | null;
  maxScore: number | null;
  includeModelBoxes: boolean;
  seed: number | null;
  limit: number;
}

export interface QueueCounts {
  outstanding: number;
  done: number;
  in_band: number;
}

export interface QueueResponse {
  items: ReviewFrame[];
  counts: QueueCounts;
  mode: QueueMode;
  order: QueueOrder;
  review: boolean;
  /** True when the server withheld scores and model boxes rather than the UI hiding them. */
  blind: boolean;
  seed: number;
  /**
   * Served, not hard-coded. app/detection/classes.py warns that the class list, the
   * model's data.yaml and DETECTION_CLASS_NAMES must agree BY POSITION, and that a
   * mismatch means server_probability can be sourced from the wrong class — which
   * fusion cannot detect because it never sees a class. A copy in TypeScript would
   * be a fourth artefact to drift.
   */
  classes: string[];
  primary_class_id: number;
  region_classes: string[];
  thin_aspect_ratio: number;
  generated_at: string;
}

export interface VerdictResponse {
  client_id: string;
  label: number;
  note: string | null;
  labeled_by: string;
  labeled_at: string;
}

export interface BoxesResponse {
  client_id: string;
  boxes: NormBox[];
  /** Always null: saving is not submitting. */
  boxed_at: string | null;
  thin_warnings: string[];
}

export interface SubmitResponse {
  finalized: number;
  already_finalized: number;
  skipped_unjudged: string[];
  /**
   * Judged but never boxed. The server refuses these because signing one off would
   * assert "reviewed, genuinely clean" about an image nobody opened, and the exporter
   * would ship it as a YOLO background image.
   */
  skipped_undrafted: string[];
}

const BASE = '/api/v1/review';

export async function fetchQueue(p: QueueParams, signal?: AbortSignal): Promise<QueueResponse> {
  const q = new URLSearchParams({
    mode: p.mode,
    order: p.order,
    review: String(p.review),
    include_model_boxes: String(p.includeModelBoxes),
    limit: String(p.limit),
  });
  // Sent only when set: min_score=0 is meaningful and must survive, so the guard is
  // `!== null` rather than a falsy check.
  if (p.minScore !== null) q.set('min_score', String(p.minScore));
  if (p.maxScore !== null) q.set('max_score', String(p.maxScore));
  if (p.seed !== null) q.set('seed', String(p.seed));

  const res = await request(`${BASE}/frames?${q.toString()}`, { signal });
  return (await res.json()) as QueueResponse;
}

const JSON_POST = { method: 'POST', headers: { 'Content-Type': 'application/json' } } as const;

export async function postVerdict(
  clientId: string,
  label: number,
  note: string | null,
): Promise<VerdictResponse> {
  const res = await request(`${BASE}/frames/${encodeURIComponent(clientId)}/verdict`, {
    ...JSON_POST,
    body: JSON.stringify({ label, note }),
  });
  return (await res.json()) as VerdictResponse;
}

/**
 * Replace this frame's boxes, saving a DRAFT.
 *
 * Deliberately carries no AbortSignal. This is a replace-all write; aborting it
 * would leave the caller unable to say whether it landed, which is worse than
 * waiting for it.
 */
export async function postBoxes(clientId: string, boxes: NormBox[]): Promise<BoxesResponse> {
  const res = await request(`${BASE}/frames/${encodeURIComponent(clientId)}/boxes`, {
    ...JSON_POST,
    body: JSON.stringify({ boxes }),
  });
  return (await res.json()) as BoxesResponse;
}

/** The server caps a batch at 200, so chunk rather than 422 on a long session. */
export const SUBMIT_CHUNK = 200;

export async function postSubmit(clientIds: string[]): Promise<SubmitResponse> {
  const total: SubmitResponse = {
    finalized: 0,
    already_finalized: 0,
    skipped_unjudged: [],
    skipped_undrafted: [],
  };
  for (let i = 0; i < clientIds.length; i += SUBMIT_CHUNK) {
    const chunk = clientIds.slice(i, i + SUBMIT_CHUNK);
    const res = await request(`${BASE}/frames/boxes/submit`, {
      ...JSON_POST,
      body: JSON.stringify({ client_ids: chunk }),
    });
    const part = (await res.json()) as SubmitResponse;
    total.finalized += part.finalized;
    total.already_finalized += part.already_finalized;
    total.skipped_unjudged.push(...part.skipped_unjudged);
    total.skipped_undrafted.push(...part.skipped_undrafted);
  }
  return total;
}
