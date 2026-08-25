"""Label real frames by hand, to measure the detector against ground truth.

Phase 2.7. Serves a one-key-per-frame page on localhost and writes `frame_label`
(migration 010). Nothing else is written — no asset_frame column is touched.

    python scripts/label_frames.py --count 300 --by "sean"
    ... --port 8020        different port
    ... --all              ignore the stratified sample, work through every frame
    ... --review           re-show frames already labelled

Keys:  1 = pothole   0 = not a pothole   u = unsure/can't tell
       b = toggle the on-device boxes    j / k = next / previous

Two deliberate choices worth knowing about:

**The device's boxes are hidden by default.** Showing a model's guess while a human
decides the ground truth anchors the human to the model, and the resulting labels
would flatter whatever produced them. Press `b` when you genuinely want to see what
the phone thought, ideally after deciding.

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
from collections import defaultdict
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import urlsplit

# Run directly (`python scripts/label_frames.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool, run_migrations  # noqa: E402
from app.services.frame_service import resolve_local_frame_path  # noqa: E402

# Inverse of the test guard: these are the databases this script must NOT write to.
_TEST_DATABASES = frozenset({"pothole_test", "pothole_ci"})

# Local hours (UTC-4, Toronto) counted as daylight. Night frames are a different
# detection problem -- rain on glass, headlight glare -- and must be measured apart.
_DAY_HOURS = range(6, 20)
_LOCAL_OFFSET_HOURS = 4

_SELECT_SQL = """
SELECT f.client_id, f.jpeg_url, f.device_probability, f.device_detections, f.ts_utc,
       l.label
FROM asset_frame f
LEFT JOIN frame_label l ON l.frame_client_id = f.client_id
ORDER BY f.received_at ASC, f.client_id ASC
"""

_UPSERT_SQL = """
INSERT INTO frame_label (frame_client_id, label, labeled_by, note)
VALUES ($1, $2, $3, $4)
ON CONFLICT (frame_client_id) DO UPDATE
SET label = EXCLUDED.label, labeled_by = EXCLUDED.labeled_by,
    labeled_at = now(), note = EXCLUDED.note
"""


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


# ── Queue building ────────────────────────────────────────────────────────────


def _stratum(row) -> tuple[int, str]:
    p = row["device_probability"]
    decile = 0 if p is None else min(9, int(p * 10))
    hour = (row["ts_utc"].hour - _LOCAL_OFFSET_HOURS) % 24
    return decile, "day" if hour in _DAY_HOURS else "night"


def _stratified(rows: list, count: int) -> list:
    """Round-robin across (decile, day/night) buckets until `count` is reached."""
    buckets: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        buckets[_stratum(r)].append(r)

    picked, keys = [], sorted(buckets)
    i = 0
    while len(picked) < count and any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            picked.append(buckets[k].pop(0))
        i += 1
    return picked


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

    usable, missing = [], 0
    for r in rows:
        if r["label"] is not None and not args.review:
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
    if args.all:
        return usable
    return _stratified(usable, args.count)


async def _save(client_id: str, label: int, by: str, note: str | None) -> None:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            await conn.execute(_UPSERT_SQL, client_id, label, by, note)
    finally:
        await pool.close()


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
 aside { min-width:280px; }
 dl { display:grid; grid-template-columns:auto 1fr; gap:2px 12px; margin:0 0 18px; }
 dt { color:#9b9284; } dd { margin:0; font-variant-numeric:tabular-nums; }
 button { font:inherit; padding:9px 14px; margin:0 6px 6px 0; border-radius:8px;
          border:1px solid #4a4335; background:#241f19; color:#f4efe6; cursor:pointer; }
 button:hover { background:#332c22; }
 kbd { background:#332c22; border-radius:4px; padding:1px 5px; font-size:12px; }
 #done { color:#8fbf7a; } #note { color:#9b9284; font-size:12px; margin-top:14px; }
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
    <div id="note">
      <kbd>b</kbd> toggle the on-device boxes &mdash; hidden by default so the
      model's guess doesn't anchor yours. <kbd>j</kbd>/<kbd>k</kbd> move without
      labelling.
    </div>
  </aside>
</div>
<script>
let queue = [], i = 0, showBoxes = false;

function setText(id, value) { document.getElementById(id).textContent = value; }

function render() {
  const f = queue[i];
  if (!f) { setText('done', 'All done \\u2014 nothing left in the queue.'); return; }
  setText('progress', `${i + 1} / ${queue.length}`);
  setText('cid', f.client_id);
  setText('ts', f.ts);
  setText('dev', f.device_probability === null ? '\\u2014'
                 : f.device_probability.toFixed(3));
  setText('existing', f.label === null ? '\\u2014' : labelName(f.label));
  const img = document.getElementById('img');
  img.onload = drawBoxes;
  img.src = `/frame/${encodeURIComponent(f.client_id)}`;
}

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

async function label(value) {
  const f = queue[i];
  if (!f) return;
  await fetch('/api/label', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ client_id: f.client_id, label: value }),
  });
  f.label = value;
  i = Math.min(i + 1, queue.length);
  render();
}

document.querySelectorAll('button[data-label]').forEach(function (b) {
  b.onclick = function () { label(parseInt(b.dataset.label, 10)); };
});

document.addEventListener('keydown', function (e) {
  if (e.key === '1') label(1);
  else if (e.key === '0') label(0);
  else if (e.key === 'u') label(-1);
  else if (e.key === 'b') { showBoxes = !showBoxes; drawBoxes(); }
  else if (e.key === 'j') { i = Math.min(i + 1, queue.length - 1); render(); }
  else if (e.key === 'k') { i = Math.max(i - 1, 0); render(); }
});

fetch('/api/queue').then(function (r) { return r.json(); })
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


def _handler(queue: list[dict], by: str):
    index = {f["client_id"]: f for f in queue}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):  # quiet: one line per label is enough
            pass

        def _send(self, code, body, content_type):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802 — BaseHTTPRequestHandler's interface
            if self.path == "/":
                return self._send(200, PAGE.encode("utf-8"), "text/html; charset=utf-8")
            if self.path == "/api/queue":
                payload = [
                    {
                        "client_id": f["client_id"],
                        "device_probability": f["device_probability"],
                        "ts": f["ts_utc"].isoformat(timespec="seconds"),
                        "label": f["label"],
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
            if self.path != "/api/label":
                return self._send(404, b"not found", "text/plain")
            length = int(self.headers.get("Content-Length") or 0)
            try:
                payload = json.loads(self.rfile.read(length))
                client_id = payload["client_id"]
                label = int(payload["label"])
            except (TypeError, ValueError, KeyError):
                return self._send(400, b"bad request", "text/plain")
            if client_id not in index or label not in (1, 0, -1):
                return self._send(400, b"bad request", "text/plain")

            asyncio.run(_save(client_id, label, by, None))
            index[client_id]["label"] = label
            done = sum(1 for f in queue if f["label"] is not None)
            print(f"  {done}/{len(queue)}  {client_id} → {label}")
            return self._send(200, b'{"ok":true}', "application/json")

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
    args = p.parse_args()

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

    print(f"database: {db}   labeled_by: {args.by}")
    queue = asyncio.run(_load_queue(args))
    if not queue:
        print("Nothing to label — every frame with a readable JPEG already has a label.")
        return 0

    strata = len({_stratum(f) for f in queue})
    print(f"queued {len(queue)} frames across {strata} strata (device-p decile x day/night)")
    print(f"\n  open http://127.0.0.1:{args.port}/   (Ctrl-C when finished)\n")

    server = HTTPServer(("127.0.0.1", args.port), _handler(queue, args.by))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        done = sum(1 for f in queue if f["label"] is not None)
        print(f"\nStopped. {done} of {len(queue)} labelled; re-run to continue.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
