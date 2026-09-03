"""Label real frames by hand, to measure the detector against ground truth.

Phase 2.7. Serves a one-key-per-frame page on localhost and writes `frame_label`
(migration 010). Nothing else is written — no asset_frame column is touched.

    python scripts/label_frames.py --count 300 --by "sean"
    ... --port 8020        different port
    ... --all              ignore the stratified sample, work through every frame
    ... --review           check-my-work: queue ONLY frames already done

Keys:  1 = pothole   0 = not a pothole   u = unsure/can't tell
       m/s/g/w = tag the reason (manhole, tar seal, grate, wet/shadow)
       n = type a free-text reason
       b = toggle the on-device boxes
       left/right arrows (or k / j) = previous / next without labelling

BOX MODE (Phase 2.7b) writes `frame_box` (migration 013) instead of `frame_label`:

    python scripts/label_frames.py --box --ids runs/negatives-train-ids.txt --by "sean"

Keys:  1..5 = pick the class   drag = draw   click = select   Del = delete
       right/left arrows (or j / k, or Enter to go forward) = SAVE this frame as a
           draft and move -- so "next" always means "done with this one"
       Shift + arrow = move WITHOUT recording anything; Home / End = jump
       s = SUBMIT every draft        r = reload the queue from the database
       b = toggle the on-device boxes

**Box regions, not lines.** `crack` has no natural compact extent: a hairline crack
boxes to a sliver of almost entirely undamaged asphalt, and training on that teaches
the model that the asphalt IS the class -- the same suppression failure that cost
pothole recall in v2/v3, aimed at a new target. Prefer the cracked patch (alligator or
block cracking, a crazed area). A warning appears for boxes thinner than ~1:6; it is
advisory and never blocks a save, because a genuinely thin region is occasionally
right and only the person looking at the frame can tell.

Why a second mode rather than a second tool: the frame is already on screen, the
JPEG resolver and the queue are already built, and the anti-anchoring rule below has
to hold in both. Box mode only queues frames that ALREADY carry a verdict, so a box
is never drawn before a decision has been made.

**Draft, then submit.** `Enter` writes the boxes to the database straight away -- so a
crash or a Ctrl-C costs nothing -- but leaves the frame a *draft*: `frame_label.boxed_at`
stays NULL, so it is still editable and still invisible to the exporter. Move back with
`k`, fix whatever you want, then `s` signs off every draft at once. Nothing downstream
can consume a frame you have not submitted.

An interrupted session re-adopts its drafts on the next run, because a queued frame
holding boxes with no `boxed_at` can only have come from an unsubmitted save. The one
draft that cannot survive a restart is a *zero-box* one -- "I looked, there is nothing
here" leaves no trace until it is submitted -- so re-visit those.

**Saving zero boxes is a real answer.** Submitting a frame you drew nothing on records
"reviewed, genuinely clean". That is what separates a true background image from a frame
nobody has looked at, and the exporter refuses to ship the latter as training data.

**The queue holds outstanding work only.** Submitted frames leave it as soon as they
are signed off, so you never page over finished work to reach the next real frame.
`--review` is the other half of that: it queues ONLY submitted frames, for checking or
correcting a pass. The two never mix.

A queue is a snapshot taken at startup, so anything written behind it -- a second
session, a `--reset-reviewed` run -- leaves it stale. `r` re-reads it in place rather
than making you restart. Running one session at a time avoids the problem entirely.

Botched a pass? `--box --ids <file> --reset-reviewed` un-submits those frames so they
return to the queue. It clears the marker only; boxes are kept.

Two deliberate choices worth knowing about:

**The device's boxes are hidden by default.** Showing a model's guess while a human
decides the ground truth anchors the human to the model, and the resulting labels
would flatter whatever produced them. Press `b` when you genuinely want to see what
the phone thought, ideally after deciding.

**A negative can carry a reason, and should.** `label = 0` alone collapses clean
asphalt and an uneven manhole into one value, and that distinction cannot be
recovered afterwards without relabelling every negative by hand. The reason lands in
`frame_label.note`, which existed from migration 010 but had no way to be filled. It
is optional and costs one keystroke. It matters because a manhole is a *hard*
negative -- dark, roughly round, on road surface -- so training on it as background
buys precision at some risk to recall, and the note is what makes that measurable
rather than a guess. It also keeps the option open of promoting a category to its own
class later, which is the right answer if uneven manholes turn out to be reportable
defects in their own right.

**The sample is stratified, not random.** 2916 frames with a median
device_probability of 0.118 means a random 300 would be ~300 negatives, which
measures recall not at all. The queue takes frames from every device_probability
decile and balances day against night, so both the "obvious" and the "hard" ends
of the distribution are represented. That makes the raw positive rate in the
labelled set NOT an estimate of the real-world positive rate -- it is a
deliberately enriched evaluation set, and any prevalence claim needs reweighting.

Writes to the DEV database by default, which is where the real frames are. That is
the one database tests must never touch (they TRUNCATE), so the guard here is the
inverse of tests/conftest.py's and scripts/seed_demo.py's: test databases are
refused unless --allow-test-db is passed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Run directly (`python scripts/label_frames.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, and several messages below contain an arrow
# or an em dash. Without this the script raises UnicodeEncodeError when it prints
# them -- which on this path means *after* every frame has already been scored.
# Harmless on POSIX, where stdout is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from app.config import settings  # noqa: E402
from app.database import create_pool, run_migrations  # noqa: E402
from app.detection.classes import (  # noqa: E402
    REGION_CLASSES,
    ROAD_SURFACE_CLASSES,
    THIN_ASPECT_RATIO,
    is_thin,
)
from app.services.frame_service import resolve_local_frame_path  # noqa: E402
from app.services.label_queue import (  # noqa: E402
    count_scored,
    rank_by_score,
)
from app.services.label_queue import (
    is_done as _is_done,
)
from app.services.label_queue import (
    stratified as _stratified,
)
from app.services.label_queue import (
    stratum as _stratum,
)
from app.services.label_queue import (
    wants_frame as _wants_frame,
)

# Inverse of the test guard: these are the databases this script must NOT write to.
_TEST_DATABASES = frozenset({"pothole_test", "pothole_ci"})

_SELECT_SQL = """
SELECT f.client_id, f.jpeg_url, f.device_probability, f.device_detections, f.ts_utc,
       f.server_probability,
       l.label, l.note, l.boxed_at,
       COALESCE((
           SELECT json_agg(json_build_object(
                      'class_id', b.class_id, 'x', b.x, 'y', b.y, 'w', b.w, 'h', b.h)
                  ORDER BY b.id)
           FROM frame_box b WHERE b.frame_client_id = f.client_id
       ), '[]'::json)::text AS human_boxes
FROM asset_frame f
LEFT JOIN frame_label l ON l.frame_client_id = f.client_id
ORDER BY f.received_at ASC, f.client_id ASC
"""

# A note is free text, so it gets a ceiling before it reaches the database. 200 is
# far more than "manhole" needs and far less than anything worth storing here.
_MAX_NOTE = 200

_UPSERT_SQL = """
INSERT INTO frame_label (frame_client_id, label, labeled_by, note)
VALUES ($1, $2, $3, $4)
ON CONFLICT (frame_client_id) DO UPDATE
SET label = EXCLUDED.label, labeled_by = EXCLUDED.labeled_by,
    labeled_at = now(), note = EXCLUDED.note
"""

# Boxes are replace-on-save, not upsert: a frame holds many of them and the surrogate
# key is meaningless to the client, so "these are the boxes now" is the only sane
# contract. Deleting and re-inserting inside one transaction keeps a half-saved frame
# from ever being visible.
_DELETE_BOXES_SQL = "DELETE FROM frame_box WHERE frame_client_id = $1"

_INSERT_BOX_SQL = """
INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by)
VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

# Finalizing is SEPARATE from saving. Boxes hit the database the moment they are
# drawn, so a crash or a Ctrl-C loses nothing -- but the frame stays a draft until the
# operator submits. That is what makes going back and correcting an earlier frame safe:
# nothing downstream can consume a frame the operator has not signed off, because the
# exporter keys on boxed_at and a draft has none.
#
# Set even when zero boxes were drawn -- that IS the "reviewed, clean" signal, and it is
# the whole reason this marker cannot simply be "does the frame have boxes".
_MARK_BOXED_SQL = """
UPDATE frame_label SET boxed_at = now()
WHERE frame_client_id = ANY($1::text[])
"""

# Undo a finalize. Boxes are deliberately NOT touched: the marker says "a human signed
# this off", and clearing it returns the frame to the queue with its work intact.
_UNMARK_BOXED_SQL = """
UPDATE frame_label SET boxed_at = NULL
WHERE frame_client_id = ANY($1::text[]) AND boxed_at IS NOT NULL
"""


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


# ── Queue building ────────────────────────────────────────────────────────────


async def _load_queue(args) -> list[dict]:
    pool = await create_pool()
    try:
        # frame_label arrives in migration 010, and the dev database only gets
        # migrated when the API boots. Applying them here is safe and is what
        # scripts/seed_demo.py already does: every migration is additive and the
        # schema_migrations ledger (Phase 2.6) makes each one run at most once.
        await run_migrations(pool)
        async with pool.acquire() as conn:
            rows = await conn.fetch(_SELECT_SQL)
    finally:
        await pool.close()

    only = _id_filter(args)
    usable, missing, done = [], 0, 0
    for r in rows:
        if only is not None and r["client_id"] not in only:
            continue
        sp = r["server_probability"]
        if getattr(args, "min_score", None) is not None and (sp is None or sp < args.min_score):
            continue
        if getattr(args, "max_score", None) is not None and (sp is None or sp >= args.max_score):
            continue
        if not _wants_frame(r, box=args.box, review=args.review):
            # Counted so the startup line can explain a short queue rather than
            # leaving "queued 20" to look like frames went missing.
            if _is_done(r, box=args.box):
                done += 1
            continue
        try:
            path = resolve_local_frame_path(r["jpeg_url"])
        except Exception:  # noqa: BLE001 — a bad path is a skipped frame, not a crash
            missing += 1
            continue
        if not path.exists():
            missing += 1
            continue
        usable.append({**dict(r), "path": path})

    if missing:
        print(f"  {missing} frames skipped: no readable JPEG on disk")
    if done:
        what = "already submitted" if args.box else "already labelled"
        print(f"  {done} frames skipped: {what}"
              + ("" if args.review else "   (--review to work through those instead)"))
    # An explicit id list is already a deliberate selection; stratifying or
    # truncating it would silently drop frames the caller asked for by name.
    if args.all or args.box or only is not None:
        return usable
    if getattr(args, "order", "stratified") == "score":
        return _by_score(usable, args.count)
    return _stratified(usable, args.count)


def _by_score(rows: list, count: int) -> list:
    """CLI wrapper over label_queue.rank_by_score that reports what it dropped.

    The ranking itself lives in app/services/label_queue.py so the console's review
    queue orders frames identically. Only the operator-facing reporting is here: a
    service module called from a request handler must not print, and the "run the
    backfill first" message is the thing that stops a silent empty queue looking like
    a finished corpus.
    """
    ranked = rank_by_score(rows, count)
    n_scored = count_scored(rows)
    if n_scored == 0:
        print("  no frame carries a server_probability -- run "
              "scripts/backfill_detection.py first, or use --order stratified",
              file=sys.stderr)
        return []
    if n_scored < len(rows):
        print(f"  {len(rows) - n_scored} frame(s) are unscored and excluded from "
              f"--order score")
    return ranked


def _id_filter(args) -> set[str] | None:
    """Restrict the queue to a newline-delimited client_id file, or None for no filter.

    This is how a run is scoped to frames that are already in a training split --
    e.g. runs/negatives-train-ids.txt -- so that annotating them cannot disturb the
    holdout the previous models were measured on. Same file format
    scripts/detect_eval.py --exclude-ids reads.
    """
    if not getattr(args, "ids", None):
        return None
    ids = {
        line.strip()
        for line in Path(args.ids).read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    print(f"  restricted to {len(ids)} id(s) from {args.ids}")
    return ids


async def _save(client_id: str, label: int, by: str, note: str | None) -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(_UPSERT_SQL, client_id, label, by, note)
    finally:
        await pool.close()


async def _save_boxes(client_id: str, boxes: list[dict], by: str) -> None:
    """Replace this frame's boxes. Does NOT finalize -- see _finalize.

    Writing immediately means a crash or a Ctrl-C costs nothing, while leaving
    `boxed_at` alone means the frame is still editable and still invisible to the
    exporter. An empty `boxes` is valid: it clears a previous annotation.
    """
    pool = await create_pool()
    try:
        async with pool.acquire() as conn, conn.transaction():
            await conn.execute(_DELETE_BOXES_SQL, client_id)
            for b in boxes:
                await conn.execute(
                    _INSERT_BOX_SQL, client_id, b["class_id"],
                    b["x"], b["y"], b["w"], b["h"], by,
                )
    finally:
        await pool.close()


async def _finalize(client_ids: list[str]) -> int:
    """Sign off a batch of drafts in one transaction. Returns the row count."""
    if not client_ids:
        return 0
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(_MARK_BOXED_SQL, client_ids)
    finally:
        await pool.close()
    return int(result.split()[-1])


async def _unfinalize(client_ids: list[str]) -> int:
    """Return finalized frames to draft. Boxes are kept."""
    if not client_ids:
        return 0
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            result = await conn.execute(_UNMARK_BOXED_SQL, client_ids)
    finally:
        await pool.close()
    return int(result.split()[-1])


# ── HTTP ──────────────────────────────────────────────────────────────────────

PAGE = """<!doctype html>
<meta charset="utf-8"><title>Frame labelling</title>
<style>
 :root { color-scheme: dark; }
 body { margin:0; background:#16140f; color:#f4efe6;
        font:14px/1.5 ui-sans-serif,system-ui,sans-serif; }
 header { display:flex; gap:18px; align-items:baseline; padding:10px 16px;
          border-bottom:1px solid #322d24; }
 h1 { font-size:14px; margin:0; letter-spacing:.08em; text-transform:uppercase;
      color:#c98a5a; }
 #wrap { display:flex; gap:24px; padding:16px; align-items:flex-start; }
 #stage { position:relative; line-height:0; }
 #stage img { max-height:76vh; border-radius:8px; }
 .box { position:absolute; border:2px solid #ff5a4a; border-radius:2px;
        box-shadow:0 0 0 1px rgba(0,0,0,.5); }
 .box span { position:absolute; top:-16px; left:0; font-size:11px; color:#ffd84a; }
 /* Human boxes are a separate class from .box on purpose: drawBoxes() wipes every
    .box on each call, and a toggle mid-draw must not destroy work in progress. */
 .hbox { position:absolute; border:2px solid #4aa3ff; border-radius:2px;
         box-shadow:0 0 0 1px rgba(0,0,0,.5); }
 .hbox.sel { border-style:dashed; box-shadow:0 0 0 2px rgba(255,255,255,.55); }
 .hbox span { position:absolute; top:-16px; left:0; font-size:11px;
              background:rgba(0,0,0,.55); padding:0 3px; border-radius:3px; }
 #stage.drawing { cursor:crosshair; }
 #classes button.on { background:#3d2f1e; border-color:#c98a5a; color:#f0c08a; }
 #classes button { padding:5px 10px; font-size:13px; }
 #classes .swatch { display:inline-block; width:9px; height:9px; border-radius:2px;
                    margin-right:6px; vertical-align:middle; }
 aside { min-width:280px; }
 dl { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; margin:0 0 18px; }
 dt { color:#9b9284; } dd { margin:0; font-variant-numeric:tabular-nums; }
 button { font:inherit; padding:9px 14px; margin:0 6px 6px 0; border-radius:8px;
          border:1px solid #4a4335; background:#241f19; color:#f4efe6; cursor:pointer; }
 button:hover { background:#332c22; }
 kbd { background:#332c22; border-radius:4px; padding:1px 5px; font-size:12px; }
 #done { color:#8fbf7a; } #note { color:#9b9284; font-size:12px; margin-top:14px; }
 #reasons, #boxpanel { margin:14px 0 6px; padding-top:12px;
                       border-top:1px solid #322d24; }
 #reasons h2, #boxpanel h2 { font-size:11px; margin:0 0 8px; letter-spacing:.08em;
               text-transform:uppercase; color:#9b9284; font-weight:600; }
 #boxnote { color:#9b9284; font-size:12px; margin-top:10px; }
 #boxwarn { margin-top:10px; padding:8px 10px; border-radius:8px; font-size:12px;
            background:#3a2a14; border:1px solid #b8862f; color:#f0c88a; }
 #submitbtn { border-color:#6d8f5e; }
 #submitbtn.pending { background:#2c3a24; border-color:#8fbf7a; color:#cfe6c2; }
 #draftline { margin-top:8px; font-size:12px; color:#9b9284; }
 #nav { display:flex; gap:6px; margin-bottom:8px; }
 #nav button { flex:1; margin:0; }
 #nav button:disabled { opacity:.35; cursor:default; }
 #stage.done img { outline:2px solid #8fbf7a; outline-offset:3px; }
 #reasons button { padding:5px 10px; font-size:13px; }
 #reasons button.on { background:#3d2f1e; border-color:#c98a5a; color:#f0c08a; }
 #notetext { width:100%; box-sizing:border-box; margin-top:6px; padding:7px 9px;
             border-radius:8px; border:1px solid #4a4335; background:#1c1813;
             color:#f4efe6; font:inherit; }
 #notetext:focus { outline:none; border-color:#c98a5a; }
</style>
<header>
  <h1>Frame labelling</h1>
  <span id="progress"></span>
  <span id="done"></span>
</header>
<div id="wrap">
  <div id="stage"><img id="img" alt="road frame"></div>
  <aside>
    <dl>
      <dt>frame</dt><dd id="cid" style="font-family:ui-monospace,monospace"></dd>
      <dt>captured</dt><dd id="ts"></dd>
      <dt>device p</dt><dd id="dev"></dd>
      <dt>existing</dt><dd id="existing"></dd>
    </dl>
    <button data-label="1">Pothole <kbd>1</kbd></button>
    <button data-label="0">Not a pothole <kbd>0</kbd></button>
    <button data-label="-1">Can't tell <kbd>u</kbd></button>
    <div id="boxpanel" hidden>
      <h2 id="classhead">Class</h2>
      <div id="classes"></div>
      <div id="nav">
        <button id="prevbtn" title="save this frame and go back one">&larr; Save &amp;
          back <kbd>&larr;</kbd></button>
        <button id="nextbtn" title="save this frame; move on if there is one after it"
          ><span id="nextlabel">Save &amp; next &rarr;</span> <kbd>&rarr;</kbd></button>
      </div>
      <button id="submitbtn">Submit all <kbd>s</kbd></button>
      <button id="reloadbtn" title="re-read the queue from the database">Reload
        <kbd>r</kbd></button>
      <div id="draftline"></div>
      <div id="boxwarn" hidden></div>
      <div id="boxnote">
        Drag on the image to draw. Click a box to select it, <kbd>Del</kbd> removes it.
        <kbd>Enter</kbd> saves and moves on &mdash; <strong>including when you drew
        nothing</strong>, which records "reviewed, genuinely clean". That is what
        separates a real background image from a frame nobody has opened, and only
        the former may be used for training.
        <br><br>
        <strong>Moving saves.</strong> <kbd>&rarr;</kbd> and <kbd>&larr;</kbd> (or
        <kbd>j</kbd>/<kbd>k</kbd>, or <kbd>Enter</kbd> for forward) record the frame you
        are leaving and move on. Revisiting reloads what you drew, so going back to fix
        something costs nothing.
        <br><br>
        <strong>Hold <kbd>Shift</kbd> to look without recording</strong>, and use
        <kbd>Home</kbd>/<kbd>End</kbd> to jump. Worth knowing why: a frame you leave
        with no boxes becomes "reviewed, genuinely clean" on submit, so skimming past
        frames you never really looked at would hand them to the trainer as background.
        <br><br>
        <strong>Nothing is final until you Submit.</strong> Saves are written to the
        database immediately, so a crash costs nothing, but each frame stays a
        <em>draft</em> &mdash; still editable, still invisible to the exporter &mdash;
        until <kbd>s</kbd> signs them all off. Submit includes the frame you are
        looking at, so the last one in a queue is never stranded &mdash; unless you
        arrived on it with <kbd>Shift</kbd> and did not touch it.
      </div>
    </div>
    <div id="reasons">
      <h2>Why (optional) &mdash; attaches to the next label</h2>
      <button data-note="manhole">manhole <kbd>m</kbd></button>
      <button data-note="tar seal">tar seal <kbd>s</kbd></button>
      <button data-note="grate">grate <kbd>g</kbd></button>
      <button data-note="wet/shadow">wet/shadow <kbd>w</kbd></button>
      <input id="notetext" maxlength="200" placeholder="or type a reason -- press n">
    </div>
    <div id="note">
      <kbd>b</kbd> toggle the on-device boxes &mdash; hidden by default so the
      model's guess doesn't anchor yours. <kbd>j</kbd>/<kbd>k</kbd> move without
      labelling. A reason is never required; it costs one keystroke and is the only
      way to tell an uneven manhole from clean asphalt after the fact.
    </div>
  </aside>
</div>
<script>
let queue = [], i = 0, showBoxes = false, note = '';
// Box mode state. `human` is the CURRENT frame's boxes in normalized corner-origin
// form -- the same convention the database stores and the device emits, so nothing
// converts until export.
let boxMode = false, classes = [], activeClass = 0, human = [], selected = -1;
let regionClasses = [], thinRatio = 6.0, drafted = {}, reviewMode = false;
// client_id of a frame reached by a Shift-move and not touched since. Submit
// skips it: Shift exists so skimming ahead does not record frames you never
// really looked at, and an untouched frame signs off as "reviewed, genuinely
// clean" -- a background image handed straight to the trainer.
let peeked = null;
const CLASS_COLORS = ['#ff5a4a', '#4aa3ff', '#b98aff', '#ffd84a', '#7ad19b'];

function classColor(id) { return CLASS_COLORS[id % CLASS_COLORS.length]; }

function setText(id, value) { document.getElementById(id).textContent = value; }

// textContent everywhere, never innerHTML: a note is operator free text that this
// page echoes straight back.
function setNote(v) {
  note = v;
  document.getElementById('notetext').value = v;
  document.querySelectorAll('#reasons button[data-note]').forEach(function (b) {
    b.classList.toggle('on', b.dataset.note === v && v !== '');
  });
}

function render() {
  const f = queue[i];
  if (!f) { setText('done', 'All done \\u2014 nothing left in the queue.'); return; }
  setText('progress', `${i + 1} / ${queue.length}`);
  setText('cid', f.client_id);
  setText('ts', f.ts);
  setText('dev', f.device_probability === null ? '\\u2014'
                 : f.device_probability.toFixed(3));
  setText('existing', f.label === null ? '\\u2014'
                      : labelName(f.label) + (f.note ? ' \\u2014 ' + f.note : ''));
  if (boxMode) {
    document.getElementById('stage').classList.toggle('done', !!f.boxed_at);
    renderProgress();
  }
  // Revisiting a frame shows the reason it already carries, so `--review` can
  // correct one rather than silently blanking it.
  setNote(f.note || '');
  human = (f.human_boxes || []).map(function (b) { return Object.assign({}, b); });
  selected = -1;
  renderClasses();
  const img = document.getElementById('img');
  img.onload = redraw;
  img.src = `/frame/${encodeURIComponent(f.client_id)}`;
}

function redraw() { drawBoxes(); drawHuman(); }

// Boxes are positioned against the RENDERED image size, so a window resize desyncs
// them from the pixels underneath. Read-only that was cosmetic; while drawing it
// would mean saving coordinates that do not match what was on screen.
window.addEventListener('resize', redraw);

function labelName(v) { return v === 1 ? 'pothole' : v === 0 ? 'not' : 'unsure'; }

function drawBoxes() {
  const stage = document.getElementById('stage');
  stage.querySelectorAll('.box').forEach(function (el) { el.remove(); });
  if (!showBoxes) return;
  const img = document.getElementById('img');
  (queue[i].boxes || []).forEach(function (b) {
    const el = document.createElement('div');
    el.className = 'box';
    el.style.left = (b.x * img.clientWidth) + 'px';
    el.style.top = (b.y * img.clientHeight) + 'px';
    el.style.width = (b.w * img.clientWidth) + 'px';
    el.style.height = (b.h * img.clientHeight) + 'px';
    const tag = document.createElement('span');
    tag.textContent = b.confidence.toFixed(2);
    el.appendChild(tag);
    stage.appendChild(el);
  });
}

function renderClasses() {
  const wrap = document.getElementById('classes');
  if (!boxMode || wrap.childElementCount === classes.length) {
    wrap.querySelectorAll('button').forEach(function (b, idx) {
      b.classList.toggle('on', idx === activeClass);
    });
    if (boxMode) countLabel();
    return;
  }
  wrap.textContent = '';
  classes.forEach(function (name, idx) {
    const b = document.createElement('button');
    const sw = document.createElement('span');
    sw.className = 'swatch';
    sw.style.background = classColor(idx);
    b.appendChild(sw);
    b.appendChild(document.createTextNode(name + ' '));
    const k = document.createElement('kbd');
    k.textContent = String(idx + 1);
    b.appendChild(k);
    b.classList.toggle('on', idx === activeClass);
    b.onclick = function () { activeClass = idx; renderClasses(); };
    wrap.appendChild(b);
  });
  countLabel();
}

function countLabel() {
  const f = queue[i];
  const seen = f && f.boxed_at ? ' (reviewed)' : '';
  setText('classhead', 'Class -- ' + human.length + ' box(es)' + seen);
}

function isThin(b) {
  const short = Math.min(b.w, b.h), long = Math.max(b.w, b.h);
  return short > 0 && long / short > thinRatio;
}

// Advisory only -- it never blocks a save. A crack LINE boxes to a sliver of almost
// entirely undamaged asphalt, and training on that teaches "undamaged asphalt is a
// crack" -- the same suppression failure that cost pothole recall in v2 and v3, aimed
// at a new class. But a genuinely thin region is occasionally the right call, and only
// the person looking at the frame can tell, so this informs rather than refuses.
function checkThin() {
  const warn = document.getElementById('boxwarn');
  const thin = human.filter(function (b) {
    return regionClasses.indexOf(classes[b.class_id]) !== -1 && isThin(b);
  });
  if (!thin.length) { warn.hidden = true; warn.textContent = ''; return; }
  const names = thin.map(function (b) { return classes[b.class_id]; });
  const unique = names.filter(function (n, idx) { return names.indexOf(n) === idx; });
  warn.hidden = false;
  // textContent, never innerHTML -- class names arrive from the command line.
  warn.textContent =
    thin.length + ' thin ' + unique.join('/') + ' box(es). Box REGIONS, not LINES: a '
    + 'sliver around a hairline crack is mostly undamaged asphalt, and the model learns '
    + 'that the asphalt IS the class. Prefer the cracked patch. Saving anyway is fine if '
    + 'the damage really is that shape.';
}

function drawHuman() {
  const stage = document.getElementById('stage');
  stage.querySelectorAll('.hbox').forEach(function (el) { el.remove(); });
  if (!boxMode) return;
  const img = document.getElementById('img');
  human.forEach(function (b, idx) {
    const el = document.createElement('div');
    el.className = 'hbox' + (idx === selected ? ' sel' : '');
    el.style.left = (b.x * img.clientWidth) + 'px';
    el.style.top = (b.y * img.clientHeight) + 'px';
    el.style.width = (b.w * img.clientWidth) + 'px';
    el.style.height = (b.h * img.clientHeight) + 'px';
    el.style.borderColor = classColor(b.class_id);
    const tag = document.createElement('span');
    // textContent, never innerHTML: class names come from the command line.
    tag.textContent = classes[b.class_id] || ('class ' + b.class_id);
    tag.style.color = classColor(b.class_id);
    el.appendChild(tag);
    el.onmousedown = function (ev) {
      // Selecting an existing box must not also start drawing a new one on top.
      ev.stopPropagation();
      selected = idx;
      drawHuman();
    };
    stage.appendChild(el);
  });
  countLabel();
  checkThin();
}

// Drag to draw. Coordinates come from the image's bounding rect rather than
// offsetX/offsetY, which are relative to whatever element is under the cursor --
// an existing box, usually, which would silently shift every rectangle drawn
// over another one.
(function enableDrawing() {
  const stage = document.getElementById('stage');
  let start = null, ghost = null;

  function pos(ev) {
    const img = document.getElementById('img');
    const r = img.getBoundingClientRect();
    return {
      x: Math.min(Math.max(ev.clientX - r.left, 0), r.width),
      y: Math.min(Math.max(ev.clientY - r.top, 0), r.height),
      w: r.width,
      h: r.height,
    };
  }

  stage.addEventListener('mousedown', function (ev) {
    if (!boxMode || ev.button !== 0) return;
    ev.preventDefault();
    selected = -1;
    start = pos(ev);
    ghost = document.createElement('div');
    ghost.className = 'hbox sel';
    ghost.style.borderColor = classColor(activeClass);
    stage.appendChild(ghost);
  });

  window.addEventListener('mousemove', function (ev) {
    if (!start || !ghost) return;
    const p = pos(ev);
    ghost.style.left = Math.min(start.x, p.x) + 'px';
    ghost.style.top = Math.min(start.y, p.y) + 'px';
    ghost.style.width = Math.abs(p.x - start.x) + 'px';
    ghost.style.height = Math.abs(p.y - start.y) + 'px';
  });

  window.addEventListener('mouseup', function (ev) {
    if (!start) return;
    const p = pos(ev);
    if (ghost) { ghost.remove(); ghost = null; }
    const x0 = Math.min(start.x, p.x), y0 = Math.min(start.y, p.y);
    const w = Math.abs(p.x - start.x), h = Math.abs(p.y - start.y);
    start = null;
    // A stray click is a click, not a zero-area box. The database would reject
    // w = 0 anyway; catching it here keeps the failure out of the operator's way.
    if (w < 6 || h < 6) { drawHuman(); return; }
    human.push({
      class_id: activeClass,
      x: x0 / p.w, y: y0 / p.h, w: w / p.w, h: h / p.h,
    });
    selected = human.length - 1;
    peeked = null;   // drawing on it means you looked at it
    drawHuman();
  });
})();

// Stable serialization of a box list, used only to decide whether a save would be a
// no-op. Field order is written out explicitly because the two sources of a box --
// json_build_object on the server and the drawing code here -- must not be trusted to
// agree on key order forever.
function boxKey(list) {
  return (list || []).map(function (b) {
    return [b.class_id, b.x.toFixed(6), b.y.toFixed(6),
            b.w.toFixed(6), b.h.toFixed(6)].join(',');
  }).join(';');
}

// Save the frame on screen as a draft. Returns false if the write failed, so the
// caller can refuse to navigate away from work it could not store.
async function saveCurrent() {
  const f = queue[i];
  if (!f) return true;
  // Nothing to do if this frame is already recorded and unchanged. Without this,
  // browsing back and forth would rewrite the same rows on every keystroke.
  if ((drafted[f.client_id] || f.boxed_at) && boxKey(human) === boxKey(f.human_boxes)) {
    return true;
  }
  const res = await fetch('/api/boxes', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: f.client_id, boxes: human }),
  });
  if (!res.ok) { setText('done', 'Save failed -- boxes NOT stored.'); return false; }
  f.human_boxes = human.map(function (b) { return Object.assign({}, b); });
  drafted[f.client_id] = true;
  renderProgress();
  return true;
}

// Moving normally SAVES the frame you are leaving -- that is what makes Next mean
// "done with this one". Hold Shift to browse without recording anything, which is the
// escape hatch for skimming ahead: a zero-box draft becomes "reviewed, genuinely
// clean" on submit, so arrowing past frames you never looked at would otherwise feed
// them to the trainer as background.
//
// Clamped rather than wrapping: wrapping past the end lands you on frame 1 looking
// identical to a fresh start, which is a good way to redo 40 frames by accident.
async function go(step, save) {
  if (save && !(await saveCurrent())) return;
  i = Math.min(Math.max(i + step, 0), queue.length - 1);
  peeked = save ? null : (queue[i] ? queue[i].client_id : null);
  render();
}

function draftCount() { return Object.keys(drafted).length; }

function renderProgress() {
  const d = draftCount();
  const allDrafted = queue.length > 0 && d === queue.length;
  setText('draftline', d
    ? (allDrafted
        ? 'every frame drafted — press s to submit all ' + d
        : d + ' draft(s) not submitted — press s to sign them off')
    : (reviewMode
        ? queue.length + ' submitted frame(s) — review mode'
        : (queue.length ? queue.length + ' left to submit' : 'queue empty')));
  // In box mode these buttons are ACTIONS that also move, not navigation. Being on
  // the last frame makes the move a no-op; it does not make the save one. Disabling
  // Next there is what left the final frame permanently unsaveable by mouse -- it
  // never became a draft, so Submit had no id to sign off and it never left the
  // queue. Only an empty queue disables them now.
  const atEnd = i >= queue.length - 1;
  document.getElementById('prevbtn').disabled = boxMode ? !queue.length : i <= 0;
  document.getElementById('nextbtn').disabled = boxMode ? !queue.length : atEnd;
  const nextLabel = document.getElementById('nextlabel');
  if (nextLabel) {
    nextLabel.textContent = (boxMode && atEnd && queue.length) ? 'Save · last'
                                                              : 'Save & next →';
  }
  const btn = document.getElementById('submitbtn');
  btn.classList.toggle('pending', d > 0);
  btn.textContent = d ? 'Submit ' + d + ' draft(s)' : 'Submit all';
  const k = document.createElement('kbd');
  k.textContent = 's';
  btn.appendChild(document.createTextNode(' '));
  btn.appendChild(k);
}

// Saving is a DRAFT. It writes the boxes immediately -- so a crash or a stray Ctrl-C
// costs nothing -- but does not set boxed_at, so the frame stays editable and stays
// invisible to the exporter until it is submitted.
async function saveBoxes() { await go(1, true); }

async function submitAll() {
  // Sign off what is on screen too. "I pressed submit while looking at this frame"
  // should include this frame -- not doing so is how the last one in a queue used to
  // get stranded. The peeked exception is the one case where it must not.
  const f = queue[i];
  if (f && peeked !== f.client_id && !(await saveCurrent())) return;
  if (!draftCount()) { setText('done', 'Nothing to submit.'); return; }
  const res = await fetch('/api/finalize', { method: 'POST' });
  if (!res.ok) { setText('done', 'Submit failed -- nothing was signed off.'); return; }
  const out = await res.json();
  queue.forEach(function (f) { if (drafted[f.client_id]) f.boxed_at = true; });
  drafted = {};
  // Signed-off frames leave the queue -- finished work should not be in the way of
  // the work that is left. reviewMode is the exception: there they ARE the work.
  if (!reviewMode) {
    const here = queue[i] ? queue[i].client_id : null;
    queue = queue.filter(function (f) { return !f.boxed_at; });
    // Stay where you were if that frame survived; otherwise land on what took its
    // place, which is the next thing needing attention.
    const at = queue.findIndex(function (f) { return f.client_id === here; });
    i = at >= 0 ? at : Math.min(i, Math.max(queue.length - 1, 0));
  }
  setText('done', 'Submitted ' + out.finalized + '. ' + out.remaining + ' left.');
  renderProgress();
  render();
}

// Re-read the queue from the database. Cures a snapshot gone stale behind another
// session, without losing your place any more than necessary.
async function reloadQueue() {
  if (draftCount() && !confirm(
      draftCount() + ' draft(s) are not submitted. Reloading re-reads them from the '
      + 'database -- drawn boxes survive, but a frame you left deliberately EMPTY '
      + 'has nothing stored yet and will come back as unreviewed. Reload anyway?')) {
    return;
  }
  const res = await fetch('/api/reload', { method: 'POST' });
  if (!res.ok) { setText('done', 'Reload failed.'); return; }
  const out = await res.json();
  const here = queue[i] ? queue[i].client_id : null;
  const q = await fetch('/api/queue');
  queue = await q.json();
  drafted = {};
  (out.drafted || []).forEach(function (id) { drafted[id] = true; });
  const at = queue.findIndex(function (f) { return f.client_id === here; });
  i = at >= 0 ? at : 0;
  setText('done', 'Reloaded. ' + out.remaining + ' in the queue.');
  renderProgress();
  render();
}

async function label(value) {
  const f = queue[i];
  if (!f) return;
  await fetch('/api/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: f.client_id, label: value, note: note }),
  });
  f.label = value;
  f.note = note || null;
  setNote('');            // a reason belongs to one frame, never to the next
  i = Math.min(i + 1, queue.length);
  render();
}

document.querySelectorAll('button[data-label]').forEach(function (b) {
  b.onclick = function () { label(parseInt(b.dataset.label, 10)); };
});

document.querySelectorAll('#reasons button[data-note]').forEach(function (b) {
  // Clicking the active chip clears it, so a mis-tap costs one click.
  b.onclick = function () { setNote(note === b.dataset.note ? '' : b.dataset.note); };
});

document.getElementById('notetext').oninput = function (e) { note = e.target.value; };

const REASON_KEYS = { m: 'manhole', s: 'tar seal', g: 'grate', w: 'wet/shadow' };

document.addEventListener('keydown', function (e) {
  const box = document.getElementById('notetext');
  if (e.target === box) {
    // Typing a reason must never label the frame. Enter commits and hands the
    // keyboard back; Escape abandons what was typed.
    if (e.key === 'Enter') { note = box.value; box.blur(); }
    else if (e.key === 'Escape') { setNote(''); box.blur(); }
    return;
  }
  // Box mode has its own key map. It is a separate branch rather than extra keys
  // in the shared one because '1' means two different things: "this frame is a
  // pothole" and "draw the next box as a pothole". Sharing the handler would make
  // the visible legend a lie in one mode or the other.
  if (boxMode) {
    if (e.key >= '1' && e.key <= String(Math.min(9, classes.length))) {
      activeClass = parseInt(e.key, 10) - 1;
      renderClasses();
    } else if (e.key === 'Enter') { e.preventDefault(); saveBoxes(); }
    else if (e.key === 's' || e.key === 'S') { e.preventDefault(); submitAll(); }
    else if (e.key === 'r' || e.key === 'R') { e.preventDefault(); reloadQueue(); }
    else if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault();
      if (selected >= 0) {
        human.splice(selected, 1);
        selected = -1;
        peeked = null;
        drawHuman();
      }
    } else if (e.key === 'Escape') { selected = -1; drawHuman(); }
    else if (e.key === 'b') { showBoxes = !showBoxes; drawBoxes(); }
    // Arrow keys alias j/k. The vim pair is muscle memory for some and invisible to
    // everyone else, and navigation is the one control you cannot discover by trying.
    // Shift means "just look" -- see go().
    else if (e.key === 'j' || e.key === 'ArrowRight' || e.key === 'ArrowDown') {
      e.preventDefault(); go(1, !e.shiftKey);
    } else if (e.key === 'k' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
      e.preventDefault(); go(-1, !e.shiftKey);
    } else if (e.key === 'Home') {
      // A jump crosses frames it never showed you, so it never records them.
      e.preventDefault(); i = 0; render();
    } else if (e.key === 'End') {
      e.preventDefault(); i = queue.length - 1; render();
    }
    return;
  }
  if (e.key === 'n') { e.preventDefault(); box.focus(); return; }
  if (REASON_KEYS[e.key]) {
    setNote(note === REASON_KEYS[e.key] ? '' : REASON_KEYS[e.key]);
    return;
  }
  if (e.key === '1') label(1);
  else if (e.key === '0') label(0);
  else if (e.key === 'u') label(-1);
  else if (e.key === 'b') { showBoxes = !showBoxes; drawBoxes(); }
  else if (e.key === 'j' || e.key === 'ArrowRight' || e.key === 'ArrowDown') {
    e.preventDefault(); go(1, false);
  } else if (e.key === 'k' || e.key === 'ArrowLeft' || e.key === 'ArrowUp') {
    e.preventDefault(); go(-1, false);
  }
});

document.getElementById('submitbtn').onclick = submitAll;
document.getElementById('reloadbtn').onclick = reloadQueue;
// In box mode the buttons save; in verdict mode a move is just a move, because the
// verdict keys are what record anything there.
document.getElementById('prevbtn').onclick = function (e) {
  go(-1, boxMode && !e.shiftKey);
};
document.getElementById('nextbtn').onclick = function (e) {
  go(1, boxMode && !e.shiftKey);
};

// Closing the tab with unsubmitted drafts is mostly recoverable -- the boxes are
// already in the database and the next run re-adopts them -- but a zero-box draft
// leaves no trace, so it is worth one confirm.
window.addEventListener('beforeunload', function (e) {
  if (boxMode && draftCount()) { e.preventDefault(); e.returnValue = ''; }
});

// The server decides the mode, so the page can never offer keys that would write to
// a table this run is not meant to touch.
fetch('/api/config').then(function (r) { return r.json(); }).then(function (cfg) {
  boxMode = cfg.box_mode;
  classes = cfg.classes;
  regionClasses = cfg.region_classes || [];
  thinRatio = cfg.thin_ratio || 6.0;
  (cfg.drafted || []).forEach(function (id) { drafted[id] = true; });
  reviewMode = !!cfg.review;
  if (boxMode) {
    document.getElementById('boxpanel').hidden = false;
    document.getElementById('reasons').hidden = true;
    document.getElementById('stage').classList.add('drawing');
    document.querySelectorAll('button[data-label]').forEach(function (b) {
      b.hidden = true;
    });
    setText('note', 'Box mode -- writing frame_box. The verdict is already recorded '
                  + 'and is not changed here.');
  }
  return fetch('/api/queue');
}).then(function (r) { return r.json(); })
  .then(function (q) { queue = q; render(); });
</script>
"""


def _device_boxes(raw) -> list[dict]:
    """Normalize the on-device detections into what the page draws.

    Device rows carry {"bbox": {x,y,w,h}, "confidence"}; asyncpg hands jsonb back
    as text. Anything unexpected is dropped rather than guessed at.
    """
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    boxes = []
    for d in items if isinstance(items, list) else []:
        bbox = d.get("bbox") if isinstance(d, dict) else None
        if not isinstance(bbox, dict):
            continue
        try:
            boxes.append(
                {
                    "x": float(bbox["x"]),
                    "y": float(bbox["y"]),
                    "w": float(bbox["w"]),
                    "h": float(bbox["h"]),
                    "confidence": float(d.get("confidence") or 0.0),
                }
            )
        except (KeyError, TypeError, ValueError):
            continue
    return boxes


def _handler(queue: list[dict], by: str, classes: list[str], box_mode: bool, args):
    index = {f["client_id"]: f for f in queue}

    # Frames saved but not yet signed off. Seeded from the database so a session that
    # was interrupted picks its drafts back up: a queued frame holding boxes with no
    # boxed_at can only have come from an unsubmitted save.
    #
    # The one draft that cannot survive a restart is a zero-box one -- "I looked and
    # there is nothing here" leaves no trace until it is finalized. Re-visiting such a
    # frame costs a couple of seconds, which is a better trade than inventing a third
    # persisted state to remember it.
    drafted: set[str] = {
        f["client_id"] for f in queue
        if not f["boxed_at"] and json.loads(f["human_boxes"] or "[]")
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet: one line per label is enough
            pass

        def handle_one_request(self):
            """Same reasoning as _send: an aborted request is routine here, not news.

            _send covers a client that leaves mid-body; this covers one that leaves
            while the request line or headers are being read.
            """
            try:
                super().handle_one_request()
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def _send(self, code, body, content_type):
            """Write a response, tolerating a client that has already walked away.

            Moving to the next frame reassigns img.src, and the browser aborts the
            JPEG still in flight for the previous one. Holding an arrow key makes that
            happen constantly. socketserver's default reaction is to print a full
            traceback per abort, which buries the one line per frame that actually
            matters -- and looks like a crash to anyone reading the terminal.

            The connection is gone either way; there is nothing to recover and nothing
            to report. Only these three exception types are swallowed, so a genuine
            encoding or filesystem error still surfaces.
            """
            try:
                self.send_response(code)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                self.close_connection = True

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's interface
            if self.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/config":
                body = json.dumps({
                    "box_mode": box_mode,
                    "classes": classes,
                    "drafted": sorted(drafted),
                    "review": bool(args.review),
                    # Sent rather than hardcoded in the page so the browser's warning
                    # and the server's log cannot disagree about what "thin" means.
                    "region_classes": sorted(REGION_CLASSES),
                    "thin_ratio": THIN_ASPECT_RATIO,
                })
                return self._send(200, body.encode("utf-8"), "application/json")
            if self.path == "/api/queue":
                payload = [
                    {
                        "client_id": f["client_id"],
                        "device_probability": f["device_probability"],
                        "server_probability": f["server_probability"],
                        "ts": f["ts_utc"].isoformat(timespec="seconds"),
                        "label": f["label"],
                        "note": f["note"],
                        "boxed_at": f["boxed_at"] is not None,
                        "human_boxes": json.loads(f["human_boxes"] or "[]"),
                        "boxes": _device_boxes(f["device_detections"]),
                    }
                    for f in queue
                ]
                body = json.dumps(payload).encode("utf-8")
                return self._send(200, body, "application/json")
            if self.path.startswith("/frame/"):
                # The id indexes the pre-built queue; a path from the URL is never
                # joined to the storage root (see frame_service's traversal guard).
                from urllib.parse import unquote

                frame = index.get(unquote(self.path[len("/frame/"):]))
                if frame is None:
                    return self._send(404, b"unknown frame", "text/plain")
                return self._send(200, frame["path"].read_bytes(), "image/jpeg")
            return self._send(404, b"not found", "text/plain")

        def do_POST(self):  # noqa: N802
            if self.path == "/api/boxes":
                return self._post_boxes()
            if self.path == "/api/finalize":
                return self._post_finalize()
            if self.path == "/api/reload":
                return self._post_reload()
            if self.path != "/api/label":
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length))
                client_id = payload["client_id"]
                label = int(payload["label"])
                # Optional: WHY this frame is what it is ("manhole", "tar seal").
                # A bare 0 collapses clean asphalt and an uneven manhole into the
                # same value, and that distinction cannot be recovered later
                # without relabelling every negative by hand.
                raw_note = payload.get("note")
            except (TypeError, ValueError, KeyError):
                return self._send(400, b"bad request", "text/plain")
            if client_id not in index or label not in (1, 0, -1):
                return self._send(400, b"bad request", "text/plain")
            if raw_note is not None and not isinstance(raw_note, str):
                return self._send(400, b"bad request", "text/plain")
            note = (raw_note or "").strip()[:_MAX_NOTE] or None

            asyncio.run(_save(client_id, label, by, note))
            index[client_id]["label"] = label
            index[client_id]["note"] = note
            done = sum(1 for f in queue if f["label"] is not None)
            print(f"  {done}/{len(queue)}  {client_id} → {label}"
                  + (f"  ({note})" if note else ""))
            return self._send(200, b'{"ok":true}', "application/json")

        def _post_boxes(self):
            """Store this frame's boxes, replacing whatever was there.

            The browser clamps coordinates as a convenience; this validates them as
            a guarantee. A box in pixels rather than fractions would otherwise reach
            the exporter and quietly poison a training run -- the database CHECKs
            would catch it, but as a 500 mid-session rather than a clear 400.
            """
            if not box_mode:
                # Refuse rather than 404: a stale tab from a labelling run must not
                # be able to write boxes into a session that was not started for it.
                return self._send(409, b"not in box mode", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length))
                client_id = payload["client_id"]
                raw = payload["boxes"]
            except (TypeError, ValueError, KeyError):
                return self._send(400, b"bad request", "text/plain")
            if client_id not in index or not isinstance(raw, list):
                return self._send(400, b"bad request", "text/plain")

            boxes = []
            for b in raw:
                try:
                    class_id = int(b["class_id"])
                    x, y, w, h = (float(b[k]) for k in ("x", "y", "w", "h"))
                except (TypeError, ValueError, KeyError):
                    return self._send(400, b"malformed box", "text/plain")
                if not 0 <= class_id < len(classes):
                    return self._send(400, b"unknown class_id", "text/plain")
                if not (0.0 <= x <= 1.0 and 0.0 <= y <= 1.0):
                    return self._send(400, b"box origin outside the frame", "text/plain")
                if not (0.0 < w <= 1.0 and 0.0 < h <= 1.0):
                    return self._send(400, b"box has no area", "text/plain")
                if x + w > 1.0001 or y + h > 1.0001:
                    return self._send(400, b"box extends past the frame", "text/plain")
                boxes.append({"class_id": class_id, "x": x, "y": y, "w": w, "h": h})

            asyncio.run(_save_boxes(client_id, boxes, by))
            drafted.add(client_id)
            index[client_id]["human_boxes"] = json.dumps(boxes)
            summary = ", ".join(sorted({classes[b["class_id"]] for b in boxes})) or "clean"
            final = sum(1 for f in queue if f["boxed_at"])
            print(f"  draft {len(drafted)} (+{final} submitted) / {len(queue)}  "
                  f"{client_id} → {len(boxes)} box(es)  [{summary}]")
            # Repeated in the terminal because the browser warning is easy to click
            # past, and this is the one that survives to be re-read afterwards.
            thin = [
                b for b in boxes
                if classes[b["class_id"]] in REGION_CLASSES and is_thin(b["w"], b["h"])
            ]
            if thin:
                names = ", ".join(sorted({classes[b["class_id"]] for b in thin}))
                print(f"      warning: {len(thin)} thin {names} box(es) saved. Box regions, "
                      f"not lines -- a sliver is mostly undamaged asphalt. Re-open with "
                      f"--review if that was not intended.")
            return self._send(200, json.dumps({
                "ok": True, "drafted": len(drafted),
            }).encode("utf-8"), "application/json")

        def _post_finalize(self):
            """Sign off every draft in this session.

            This is the only thing that writes `boxed_at`, and therefore the only thing
            that makes a frame visible to the exporter. Until it runs, every save is
            revisable and going back to fix an earlier frame costs nothing.
            """
            if not box_mode:
                return self._send(409, b"not in box mode", "text/plain")
            ids = sorted(drafted)
            if not ids:
                return self._send(200, b'{"ok":true,"finalized":0}', "application/json")
            count = asyncio.run(_finalize(ids))
            for cid in ids:
                index[cid]["boxed_at"] = True
            drafted.clear()
            # A signed-off frame is finished work; leaving it in the queue means
            # paging over it to reach the next real one. In --review mode the
            # signed-off frames ARE the job, so they stay.
            if not args.review:
                queue[:] = [f for f in queue if not f["boxed_at"]]
            # index deliberately keeps every frame: a /frame/<id> request already in
            # flight for one just dropped must still resolve, not 404.
            print(f"\n  SUBMITTED {count} frame(s). {len(queue)} left in this queue.")
            print("  See them again with --review; boxes are kept either way.\n")
            return self._send(200, json.dumps({
                "ok": True, "finalized": count, "remaining": len(queue),
            }).encode("utf-8"), "application/json")

        def _post_reload(self):
            """Re-read the queue from the database.

            A snapshot taken at startup goes stale the moment anything else writes --
            a second session, or a --reset-reviewed run. Without this the only cure is
            a restart, and the symptom is a queue showing work already finished.
            """
            queue[:] = asyncio.run(_load_queue(args))
            index.clear()
            index.update({f["client_id"]: f for f in queue})
            drafted.clear()
            drafted.update(
                f["client_id"] for f in queue
                if not f["boxed_at"] and json.loads(f["human_boxes"] or "[]")
            )
            print(f"  reloaded: {len(queue)} frame(s) queued, "
                  f"{len(drafted)} unsubmitted draft(s).")
            return self._send(200, json.dumps({
                "ok": True, "remaining": len(queue), "drafted": sorted(drafted),
            }).encode("utf-8"), "application/json")

    return Handler


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--count", type=int, default=300, help="stratified sample size")
    p.add_argument("--by", default="operator", help="recorded in frame_label.labeled_by")
    p.add_argument("--port", type=int, default=8020)
    p.add_argument("--all", action="store_true", help="queue every unlabelled frame")
    p.add_argument("--review", action="store_true", help="include already-labelled frames")
    p.add_argument("--allow-test-db", action="store_true",
                   help="permit a pothole_test/pothole_ci target (labels there get TRUNCATEd)")
    p.add_argument("--box", action="store_true",
                   help="draw boxes into frame_box instead of labelling (Phase 2.7b)")
    p.add_argument("--ids", help="file of client_ids, one per line: queue only these")
    p.add_argument("--reset-reviewed", action="store_true",
                   help="with --box: un-submit the frames in scope so they can be redone. "
                        "Clears frame_label.boxed_at only -- boxes are kept")
    p.add_argument("--classes", default=",".join(ROAD_SURFACE_CLASSES),
                   help="comma-separated class names for --box; position is the class_id")
    p.add_argument("--order", choices=("stratified", "score"), default="stratified",
                   help="stratified (default): device-p decile x day/night, so the set "
                        "spans lighting conditions. score: highest server_probability "
                        "first -- the fastest route to unlabelled POSITIVES, which is "
                        "what the model is short of. Requires a backfill to have run")
    p.add_argument("--min-score", type=float,
                   help="only queue frames with server_probability >= this")
    p.add_argument("--max-score", type=float,
                   help="only queue frames with server_probability < this")
    args = p.parse_args()

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    if args.box and not classes:
        print("--classes must name at least one class.", file=sys.stderr)
        return 2
    if args.ids and not Path(args.ids).exists():
        print(f"--ids file not found: {args.ids}", file=sys.stderr)
        return 2

    db = _database_name(settings.database_url)
    if db in _TEST_DATABASES and not args.allow_test_db:
        print(
            f"Refusing to label against {db!r}: the test fixtures TRUNCATE every table, "
            f"so the labels would be destroyed by the next pytest run. Point DATABASE_URL "
            f"at the database holding the real frames, or pass --allow-test-db to try the "
            f"tool out.",
            file=sys.stderr,
        )
        return 2

    print(f"database: {db}   labeled_by: {args.by}"
          + ("   mode: BOX (writes frame_box)" if args.box else ""))

    if args.reset_reviewed:
        if not args.box:
            print("--reset-reviewed only applies to --box mode.", file=sys.stderr)
            return 2
        # Scoped to --ids when given, so "undo the pass I botched" cannot become
        # "undo every review anyone has ever done".
        scope = sorted(_id_filter(args) or [])
        if not scope:
            print("--reset-reviewed needs --ids naming which frames to un-submit.",
                  file=sys.stderr)
            return 2
        cleared = asyncio.run(_unfinalize(scope))
        print(f"un-submitted {cleared} frame(s); their boxes were kept. "
              f"They are back in the queue.")
        if not cleared:
            return 0
    if args.box:
        # Printed loudly because the position of a name IS its class_id, and the
        # exporter and the decoder have to agree with this exact ordering.
        print("classes:  " + "  ".join(f"{n}={c}" for n, c in enumerate(classes, 1)))
    queue = asyncio.run(_load_queue(args))
    if not queue:
        if args.box:
            if args.review:
                print("Nothing to review: none of these frames have been submitted yet.")
            else:
                print("Nothing to box: every frame in scope is already submitted, "
                      "or none carry a verdict yet.")
                print("  Check or correct the submitted ones with --review.")
        else:
            if args.review:
                print("Nothing to review: no frames in scope carry a label yet.")
            else:
                print("Nothing to label: every frame with a readable JPEG "
                      "already has a label.")
                print("  Re-open the labelled ones with --review.")
        return 0

    if args.box:
        if args.review:
            print(f"queued {len(queue)} SUBMITTED frame(s) to review "
                  f"(review mode: outstanding frames are not shown)")
        else:
            print(f"queued {len(queue)} frame(s) still needing boxes")
    elif args.order == "score":
        sc = [f["server_probability"] for f in queue if f["server_probability"] is not None]
        rng = f"  server-p {min(sc):.3f}-{max(sc):.3f}" if sc else ""
        print(f"queued {len(queue)} frames, highest server_probability first{rng}")
        print("  scores order the queue only -- they never auto-label. The lowest band "
              "still holds real potholes.")
    else:
        strata = len({_stratum(f) for f in queue})
        print(f"queued {len(queue)} frames across {strata} strata "
              f"(device-p decile x day/night)")
    print(f"\n  open http://127.0.0.1:{args.port}/   (Ctrl-C when finished)\n")

    server = HTTPServer(("127.0.0.1", args.port),
                        _handler(queue, args.by, classes, args.box, args))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        if args.box:
            done = sum(1 for f in queue if f["boxed_at"])
            print(f"\nStopped. {done} of {len(queue)} submitted; re-run to continue.")
            print("  Any unsubmitted drafts kept their boxes and are picked back "
                  "up next run.")
        else:
            done = sum(1 for f in queue if f["label"] is not None)
            print(f"\nStopped. {done} of {len(queue)} labelled; re-run to continue.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
