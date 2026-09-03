/**
 * The review queue's state machine.
 *
 * Two failures are what this file exists to prevent, because neither is visible on
 * screen and both corrupt ground truth that feeds `scripts/promote_model.py`:
 *
 *   1. A frame leaves the queue without its boxes being stored.
 *   2. A frame gets signed off that nobody actually examined — which the exporter
 *      then ships as a YOLO background image asserting "genuinely clean road".
 */

import { describe, expect, it } from 'vitest';

import {
  advance,
  applySubmit,
  clampCursor,
  type Entry,
  needsSave,
  progressLine,
  resolveJump,
  resolveMove,
  type ReviewFrame,
  stateOf,
  submittableIds,
  toEntry,
} from './queue-state.ts';

function frame(o: Partial<ReviewFrame> = {}): ReviewFrame {
  return {
    client_id: 'f1',
    ts: null,
    image_url: '/api/v1/frames/f1/image',
    device_probability: null,
    server_probability: 0.5,
    label: null,
    note: null,
    labeled_by: null,
    labeled_at: null,
    boxed_at: null,
    boxes_drafted_at: null,
    human_boxes: [],
    server_boxes: [],
    device_boxes: [],
    vlm_verdict: null,
    ...o,
  };
}

const NOW = '2026-09-01T12:00:00Z';
const entry = (o: Partial<ReviewFrame> = {}): Entry => toEntry(frame(o));

describe('stateOf', () => {
  it('derives every state from the server fields alone', () => {
    expect(stateOf(entry())).toBe('unjudged');
    expect(stateOf(entry({ label: 1 }))).toBe('judged');
    expect(stateOf(entry({ label: 1, boxes_drafted_at: NOW }))).toBe('draft');
    expect(stateOf(entry({ label: 1, boxes_drafted_at: NOW, boxed_at: NOW }))).toBe('signed');
  });

  it('treats "unsure" as judged — a decision, not a gap', () => {
    expect(stateOf(entry({ label: -1 }))).toBe('judged');
  });

  it('reports signed off even without a draft marker, so legacy CLI rows read correctly', () => {
    expect(stateOf(entry({ label: 1, boxed_at: NOW }))).toBe('signed');
  });
});

describe('needsSave', () => {
  it('is true for a frame never drafted', () => {
    expect(needsSave(entry({ label: 1 }))).toBe(true);
  });

  it('is false when the boxes match what the server acknowledged', () => {
    const e = entry({ label: 1, boxes_drafted_at: NOW });
    expect(needsSave(e)).toBe(false);
  });

  it('is true once a box is added', () => {
    const e = entry({ label: 1, boxes_drafted_at: NOW });
    e.boxes.push({ class_id: 0, x: 0.1, y: 0.1, w: 0.2, h: 0.2 });
    expect(needsSave(e)).toBe(true);
  });

  it('is false for a frame whose image never loaded', () => {
    const e = entry({ label: 1 });
    e.imageFailed = true;
    expect(needsSave(e)).toBe(false);
  });

  it('recognises an empty draft as recorded — the whole point of boxes_drafted_at', () => {
    // "I looked, there is nothing here" is a real answer. Without the marker it is
    // indistinguishable from a frame nobody opened, and only one of those may be
    // exported as background.
    const e = entry({ label: 0, boxes_drafted_at: NOW, human_boxes: [] });
    expect(needsSave(e)).toBe(false);
  });
});

describe('submittableIds', () => {
  const drafted = (id: string) =>
    toEntry(frame({ client_id: id, label: 1, boxes_drafted_at: NOW }));

  it('includes drafts', () => {
    expect(submittableIds([drafted('a'), drafted('b')])).toEqual(['a', 'b']);
  });

  it('excludes a judged frame that was never boxed', () => {
    // The one that poisons the training set. The server refuses it too
    // (skipped_undrafted); the client must not send it in the first place.
    expect(submittableIds([toEntry(frame({ client_id: 'a', label: 1 }))])).toEqual([]);
  });

  it('excludes a frame that was only peeked at', () => {
    const e = drafted('a');
    e.peeked = true;
    expect(submittableIds([e])).toEqual([]);
  });

  it('excludes a frame whose image never loaded', () => {
    const e = drafted('a');
    e.imageFailed = true;
    expect(submittableIds([e])).toEqual([]);
  });

  it('excludes one already signed off', () => {
    expect(submittableIds([toEntry(frame({ client_id: 'a', label: 1, boxes_drafted_at: NOW, boxed_at: NOW }))])).toEqual([]);
  });

  it('never returns the whole queue just because it is the whole queue', () => {
    const mixed = [drafted('a'), toEntry(frame({ client_id: 'b', label: 1 }))];
    expect(submittableIds(mixed)).toEqual(['a']);
  });
});

describe('cursor clamping — the two modes differ deliberately', () => {
  it('box mode stops on the last frame, so current() is never null mid-save', () => {
    expect(clampCursor(99, 5, 'box')).toBe(4);
  });

  it('verdict mode may land one past the end, which is how "done" is expressed', () => {
    expect(clampCursor(99, 5, 'verdict')).toBe(5);
  });

  it('never goes negative', () => {
    expect(clampCursor(-3, 5, 'box')).toBe(0);
  });

  it('is safe on an empty queue', () => {
    expect(clampCursor(0, 0, 'box')).toBe(0);
    expect(clampCursor(3, 0, 'verdict')).toBe(0);
  });
});

describe('advance after a verdict', () => {
  it('lands one past the last index rather than sticking', () => {
    expect(advance(4, 5)).toBe(5);
    expect(advance(5, 5)).toBe(5);
  });
});

describe('resolveMove', () => {
  it('records nothing on a verdict-mode move', () => {
    expect(resolveMove(0, 5, 'verdict', 1, false)).toEqual({ cursor: 1, save: false, peek: false });
  });

  it('saves on every recording box-mode move', () => {
    expect(resolveMove(0, 5, 'box', 1, false)).toEqual({ cursor: 1, save: true, peek: false });
  });

  it('peeks without saving when shift is held', () => {
    expect(resolveMove(0, 5, 'box', 1, true)).toEqual({ cursor: 1, save: false, peek: true });
  });

  it('STILL SAVES on the last frame, where the move is a no-op', () => {
    // The recorded CLI bug: disabling Next at the end "left the final frame
    // permanently unsaveable by mouse -- it never became a draft, so Submit had no
    // id to sign off". The move being a no-op must not make the save one.
    const r = resolveMove(4, 5, 'box', 1, false);
    expect(r.cursor).toBe(4);
    expect(r.save).toBe(true);
  });

  it('still saves at the start when moving back', () => {
    const r = resolveMove(0, 5, 'box', -1, false);
    expect(r.cursor).toBe(0);
    expect(r.save).toBe(true);
  });
});

describe('resolveJump', () => {
  it('never records, because a jump crosses frames it never displayed', () => {
    expect(resolveJump('end', 5, 'box')).toEqual({ cursor: 4, save: false, peek: true });
    expect(resolveJump('start', 5, 'box')).toEqual({ cursor: 0, save: false, peek: true });
  });

  it('does not mark peeked in verdict mode, where peeking has no meaning', () => {
    expect(resolveJump('end', 5, 'verdict').peek).toBe(false);
  });
});

describe('applySubmit', () => {
  const outcome = (o: Partial<Parameters<typeof applySubmit>[2]> = {}) => ({
    finalized: 0,
    already_finalized: 0,
    skipped_unjudged: [],
    skipped_undrafted: [],
    ...o,
  });

  it('marks accepted frames signed off', () => {
    const entries = [toEntry(frame({ client_id: 'a', label: 1, boxes_drafted_at: NOW }))];
    applySubmit(entries, ['a'], outcome({ finalized: 1 }));
    expect(stateOf(entries[0]!)).toBe('signed');
  });

  it('leaves a refused frame exactly as it was', () => {
    const entries = [toEntry(frame({ client_id: 'a', label: 1 }))];
    applySubmit(entries, ['a'], outcome({ skipped_undrafted: ['a'] }));
    expect(entries[0]!.item.boxed_at).toBeNull();
    expect(stateOf(entries[0]!)).toBe('judged');
  });

  it('leaves an unjudged refusal alone too', () => {
    const entries = [toEntry(frame({ client_id: 'a' }))];
    applySubmit(entries, ['a'], outcome({ skipped_unjudged: ['a'] }));
    expect(entries[0]!.item.boxed_at).toBeNull();
  });

  it('does not touch frames that were not in the batch', () => {
    const entries = [
      toEntry(frame({ client_id: 'a', label: 1, boxes_drafted_at: NOW })),
      toEntry(frame({ client_id: 'b', label: 1, boxes_drafted_at: NOW })),
    ];
    applySubmit(entries, ['a'], outcome({ finalized: 1 }));
    expect(stateOf(entries[1]!)).toBe('draft');
  });
});

describe('progressLine', () => {
  const box = (o: { drafts: number; queueLength: number; reviewMode?: boolean }) =>
    progressLine({ reviewMode: false, mode: 'box', ...o });
  const verdict = (o: { queueLength: number; reviewMode?: boolean }) =>
    progressLine({ drafts: 0, reviewMode: false, mode: 'verdict', ...o });

  it('names the key that finishes the job', () => {
    expect(box({ drafts: 3, queueLength: 10 })).toBe(
      '3 draft(s) not submitted — press s to sign them off',
    );
  });

  it('says so when everything is drafted', () => {
    expect(box({ drafts: 10, queueLength: 10 })).toBe('every frame drafted — press s to submit all 10');
  });

  it('distinguishes review mode', () => {
    expect(box({ drafts: 0, queueLength: 4, reviewMode: true })).toBe(
      '4 submitted frame(s) — review mode',
    );
  });

  it('reports outstanding work when nothing is drafted yet', () => {
    expect(box({ drafts: 0, queueLength: 7 })).toBe('7 left to submit');
  });

  it('never says "submit" in verdict mode, which has no submit step', () => {
    // Caught in the browser: verdict mode showed "50 left to submit", sending the
    // operator looking for a button that does not exist there. The verdict keys are
    // what record.
    expect(verdict({ queueLength: 50 })).toBe('50 left to judge');
    expect(verdict({ queueLength: 4, reviewMode: true })).toBe('4 judged frame(s) — review mode');
  });

  it('says the queue is empty only when it is', () => {
    expect(box({ drafts: 0, queueLength: 0 })).toBe('queue empty');
    expect(verdict({ queueLength: 0 })).toBe('queue empty');
  });
});


// ── Box-mode transitions, as a table ──────────────────────────────────────────

describe('the box-mode transition table', () => {
  const drafted = (id: string) =>
    toEntry(frame({ client_id: id, label: 1, boxes_drafted_at: NOW }));

  it('a recording move asks for a save; a peek does not', () => {
    expect(resolveMove(1, 5, 'box', 1, false).save).toBe(true);
    expect(resolveMove(1, 5, 'box', 1, true).save).toBe(false);
  });

  it('a peek marks the destination, so it cannot later be signed off', () => {
    const entries = [drafted('a'), drafted('b')];
    const r = resolveMove(0, 2, 'box', 1, true);
    expect(r.peek).toBe(true);
    entries[r.cursor]!.peeked = true;
    expect(submittableIds(entries)).toEqual(['a']);
  });

  it('a drawn box makes the frame dirty and un-peeks it', () => {
    // Drawing on a frame means you looked at it, which is what makes it eligible
    // again after a peek.
    const e = drafted('a');
    e.peeked = true;
    e.boxes.push({ class_id: 0, x: 0.1, y: 0.1, w: 0.2, h: 0.2 });
    e.dirty = true;
    e.peeked = false;
    expect(needsSave(e)).toBe(true);
    expect(submittableIds([e])).toEqual(['a']);
  });

  it('an unchanged frame needs no write, however often it is crossed', () => {
    const e = drafted('a');
    expect(needsSave(e)).toBe(false);
    expect(needsSave(e)).toBe(false);
  });

  it('submitting drops signed frames and keeps the cursor on a survivor', () => {
    const entries = [drafted('a'), drafted('b'), drafted('c')];
    applySubmit(entries, ['a', 'c'], {
      finalized: 2,
      already_finalized: 0,
      skipped_unjudged: [],
      skipped_undrafted: [],
    });
    const here = 'b';
    const left = entries.filter((e) => e.item.boxed_at === null);
    expect(left.map((e) => e.item.client_id)).toEqual(['b']);
    expect(left.findIndex((e) => e.item.client_id === here)).toBe(0);
  });

  it('a refused frame stays in the queue so the operator can fix it', () => {
    const entries = [drafted('a'), toEntry(frame({ client_id: 'b', label: 1 }))];
    applySubmit(entries, ['a', 'b'], {
      finalized: 1,
      already_finalized: 0,
      skipped_unjudged: [],
      skipped_undrafted: ['b'],
    });
    const left = entries.filter((e) => e.item.boxed_at === null);
    expect(left.map((e) => e.item.client_id)).toEqual(['b']);
  });

  it('an empty draft is submittable — it is a real answer', () => {
    // "I looked, there is nothing here" is what separates a true background image
    // from a frame nobody opened, and only the first may be exported.
    const e = toEntry(frame({ client_id: 'a', label: 0, boxes_drafted_at: NOW, human_boxes: [] }));
    expect(e.boxes).toEqual([]);
    expect(submittableIds([e])).toEqual(['a']);
  });
});
