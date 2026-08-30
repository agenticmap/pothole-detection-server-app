"""Offline detector evaluation — run a model over stored frames, write nothing.

Phase 2.7. Two jobs:

1. **Prove the seam.** app/detection/onnx_v1.py had never executed against a real
   ONNX file. A wrong export layout or a wrong coordinate hop does not raise; it
   produces plausible boxes. Run this before enabling the worker, look at the
   annotated output, and confirm boxes land on road surface.

2. **Measure honestly.** With `--labels`, scores are compared against the human
   ground truth in `frame_label` (migration 010) and a precision/recall table is
   printed at every threshold, plus the gray-zone occupancy the VLM step needs.

This script NEVER writes to the database and never touches asset_frame — it is
safe to point at the dev database. Backfilling is scripts/backfill_detection.py.

Usage (from the repo root):

    # Prove the seam on 20 real frames, with and without the ROI crop.
    python scripts/detect_eval.py --model models/pothole.onnx --limit 20 --annotate out/
    python scripts/detect_eval.py --model models/pothole.onnx --limit 20 --no-roi

    # Measure against labels once frames have been labelled.
    python scripts/detect_eval.py --model models/pothole.onnx --labels

    # Bypass the database and just run over files on disk.
    python scripts/detect_eval.py --model models/pothole.onnx --dir storage/frames

Config is taken from CLI flags, NOT from DETECTION_* env vars, so an evaluation
run cannot be silently reading a different model than the flag says.
"""

from __future__ import annotations

import argparse
import asyncio
import random
import sys
from pathlib import Path

# Run directly (`python scripts/detect_eval.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, and several messages below contain an arrow
# or an em dash. Without this the script raises UnicodeEncodeError when it prints
# them -- which on this path means *after* every frame has already been scored.
# Harmless on POSIX, where stdout is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402
from app.detection.onnx_v1 import OnnxYoloDetector  # noqa: E402

# Frames plus their scores and (if present) the human label. LEFT JOIN on
# frame_label so --labels and plain sampling share one query.
_SELECT_FRAMES_SQL = """
SELECT f.client_id, f.jpeg_url, f.device_probability, f.ts_utc, l.label
FROM asset_frame f
LEFT JOIN frame_label l ON l.frame_client_id = f.client_id
WHERE ($1::boolean IS FALSE OR l.label IS NOT NULL)
ORDER BY f.received_at ASC, f.client_id ASC
"""

# Same shape, for a database where migration 010 hasn't been applied. $1 is kept so
# both queries take the same parameters.
_SELECT_FRAMES_NO_LABELS_SQL = """
SELECT f.client_id, f.jpeg_url, f.device_probability, f.ts_utc, NULL::smallint AS label
FROM asset_frame f
WHERE $1::boolean IS FALSE
ORDER BY f.received_at ASC, f.client_id ASC
"""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", required=True, help="path to the .onnx export")
    p.add_argument("--model-id", default="eval", help="recorded in output only")
    p.add_argument("--limit", type=int, default=0, help="0 = every frame")
    p.add_argument("--sample", action="store_true", help="random sample rather than the first N")
    p.add_argument("--seed", type=int, default=7, help="sampling seed (reproducible runs)")
    p.add_argument("--dir", help="score JPEGs under this directory instead of querying the DB")
    p.add_argument("--annotate", help="write annotated JPEGs to this directory")
    p.add_argument("--labels", action="store_true", help="only labelled frames; print metrics")
    p.add_argument("--exclude-ids",
                   help="file of client_ids (one per line) to skip -- use the training "
                        "list from export_labeled_frames.py to keep evaluation honest")
    p.add_argument("--conf", type=float, default=0.25, help="detector confidence threshold")
    p.add_argument("--iou", type=float, default=0.45, help="NMS IoU threshold")
    p.add_argument("--input-size", type=int, default=640)

    roi = p.add_mutually_exclusive_group()
    roi.add_argument(
        "--roi", dest="roi", action="store_true", help="crop to the road band (default)"
    )
    roi.add_argument("--no-roi", dest="roi", action="store_false", help="score the whole frame")
    p.set_defaults(roi=True)
    p.add_argument("--roi-top", type=float, default=0.45)
    p.add_argument("--roi-bottom", type=float, default=0.90)
    p.add_argument("--classes", default="pothole",
                   help="comma-separated class names; position is the class_id. Must match "
                        "the model's data.yaml, or boxes are mislabelled and the frame "
                        "probability can come from the wrong class")
    p.add_argument("--primary-class", type=int, default=0,
                   help="index of the class that sets the frame probability (default 0)")
    return p.parse_args()


def _build_detector(args: argparse.Namespace) -> OnnxYoloDetector:
    return OnnxYoloDetector(
        model_path=args.model,
        model_id=args.model_id,
        input_size=args.input_size,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        roi_enabled=args.roi,
        roi_top=args.roi_top,
        roi_bottom=args.roi_bottom,
        labels=[c.strip() for c in args.classes.split(",") if c.strip()],
        primary_class_id=args.primary_class,
    )


# ── Sourcing frames ───────────────────────────────────────────────────────────


async def _frames_from_db(args: argparse.Namespace) -> list[dict]:
    from app.services.frame_service import resolve_local_frame_path

    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            # frame_label ships in migration 010 and may not be applied yet. This
            # script is deliberately read-only, so it does NOT migrate — it just
            # drops the join. Only --labels actually needs the table.
            has_labels = await conn.fetchval("SELECT to_regclass('frame_label') IS NOT NULL")
            if args.labels and not has_labels:
                raise SystemExit(
                    "--labels needs the frame_label table (migration 010), which is not "
                    "applied to this database yet. Run scripts/label_frames.py once — it "
                    "applies migrations and is where labels come from."
                )
            sql = _SELECT_FRAMES_SQL if has_labels else _SELECT_FRAMES_NO_LABELS_SQL
            rows = await conn.fetch(sql, bool(args.labels))
    finally:
        await pool.close()

    # Frames that went into a model's training set must not also score it. The list
    # comes from scripts/export_labeled_frames.py, which exports the reviewed frames
    # as background images -- so without this the retrained model would be measured
    # partly on its own training data and every precision gain would be leakage.
    excluded = set()
    if getattr(args, "exclude_ids", None):
        excluded = {
            line.strip()
            for line in Path(args.exclude_ids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        print(f"excluding {len(excluded)} training frame(s) from {args.exclude_ids}")

    frames = []
    for r in rows:
        if r["client_id"] in excluded:
            continue
        try:
            path = resolve_local_frame_path(r["jpeg_url"])
        except Exception as e:  # noqa: BLE001 — a missing file is data, not a crash
            print(f"  skip {r['client_id']}: {e}", file=sys.stderr)
            continue
        if not path.exists():
            continue
        frames.append(
            {
                "client_id": r["client_id"],
                "path": path,
                "device_probability": r["device_probability"],
                "ts_utc": r["ts_utc"],
                "label": r["label"],
            }
        )
    return frames


def _frames_from_dir(args: argparse.Namespace) -> list[dict]:
    root = Path(args.dir)
    return [
        {"client_id": p.stem, "path": p, "device_probability": None, "ts_utc": None, "label": None}
        for p in sorted(root.rglob("*.jpg"))
    ]


def _select(frames: list[dict], args: argparse.Namespace) -> list[dict]:
    if args.sample:
        random.Random(args.seed).shuffle(frames)
    return frames[: args.limit] if args.limit else frames


# ── Output ────────────────────────────────────────────────────────────────────


def _annotate(path: Path, detections: list[dict], out_dir: Path, probability: float) -> None:
    """Draw the boxes back onto the frame. The visual check for the coordinate maths."""
    from PIL import Image, ImageDraw

    out_dir.mkdir(parents=True, exist_ok=True)
    img = Image.open(path).convert("RGB")
    w, h = img.size
    draw = ImageDraw.Draw(img)
    for d in detections:
        b = d.get("bbox")
        if not isinstance(b, dict):
            continue  # e.g. a hybrid _vlm_verdict entry
        x1, y1 = b["x"] * w, b["y"] * h
        x2, y2 = (b["x"] + b["w"]) * w, (b["y"] + b["h"]) * h
        draw.rectangle([x1, y1, x2, y2], outline=(255, 40, 40), width=2)
        draw.text((x1 + 2, max(0, y1 - 11)), f"{d['confidence']:.2f}", fill=(255, 220, 40))
    draw.text((4, 4), f"p={probability:.3f}", fill=(255, 220, 40))
    img.save(out_dir / f"{probability:.3f}_{path.stem}.jpg", quality=88)


def _histogram(values: list[float], bins: int = 10) -> None:
    print(f"\n  {'range':>12}  {'n':>5}")
    total = len(values)
    for i in range(bins):
        lo, hi = i / bins, (i + 1) / bins
        n = sum(1 for v in values if lo <= v < hi or (i == bins - 1 and v == 1.0))
        bar = "#" * int(round(40 * n / total)) if total else ""
        print(f"  {lo:>5.2f}-{hi:<5.2f} {n:>5}  {bar}")


def _metrics(scored: list[dict]) -> None:
    """Precision/recall at every threshold, over frames with a definite label.

    'unsure' (-1) frames are excluded from precision/recall but counted, because
    dropping them silently would flatter the result — an unreadable night frame is
    a real operating condition, not a bad row.
    """
    definite = [s for s in scored if s["label"] in (0, 1)]
    unsure = [s for s in scored if s["label"] == -1]
    positives = sum(1 for s in definite if s["label"] == 1)

    print(f"\n  Labelled: {len(definite)} definite ({positives} pothole, "
          f"{len(definite) - positives} not) + {len(unsure)} unsure")
    if not definite or not positives:
        print("  Not enough labelled positives for a precision/recall table.")
        return

    print(f"\n  {'thresh':>7} {'TP':>5} {'FP':>5} {'FN':>5} {'prec':>7} {'recall':>7} {'F1':>7}")
    best = None
    for i in range(1, 20):
        t = i / 20
        tp = sum(1 for s in definite if s["p"] >= t and s["label"] == 1)
        fp = sum(1 for s in definite if s["p"] >= t and s["label"] == 0)
        fn = sum(1 for s in definite if s["p"] < t and s["label"] == 1)
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
        print(f"  {t:>7.2f} {tp:>5} {fp:>5} {fn:>5} {prec:>7.3f} {rec:>7.3f} {f1:>7.3f}")
        if best is None or f1 > best[1]:
            best = (t, f1)
    print(f"\n  Best F1 {best[1]:.3f} at threshold {best[0]:.2f} "
          f"→ candidate DETECTION_CONF_THRESHOLD")

    if unsure:
        u = [s["p"] for s in unsure]
        print(f"  Unsure frames score mean {sum(u) / len(u):.3f}, max {max(u):.3f}")


def _gray_zone(values: list[float], low: float, high: float) -> None:
    """What fraction of frames a VLM verifier would be asked about, i.e. its cost."""
    n = sum(1 for v in values if low <= v <= high)
    pct = 100.0 * n / len(values) if values else 0.0
    print(f"\n  Gray zone [{low}, {high}]: {n} of {len(values)} frames ({pct:.1f}%) "
          f"→ VLM calls per full pass, at current bounds")


# ── Main ──────────────────────────────────────────────────────────────────────


async def main() -> int:
    args = _parse_args()
    if not Path(args.model).exists():
        print(f"Model not found: {args.model}", file=sys.stderr)
        return 2

    detector = _build_detector(args)
    frames = _select(_frames_from_dir(args) if args.dir else await _frames_from_db(args), args)
    if not frames:
        print("No frames to score.", file=sys.stderr)
        return 1

    roi = f"on ({args.roi_top}-{args.roi_bottom})" if args.roi else "off"
    source = args.dir if args.dir else f"{settings.database_url.rsplit('/', 1)[-1]} (read-only)"
    print(f"model={args.model}  conf={args.conf}  iou={args.iou}  roi={roi}")
    print(f"source={source}  frames={len(frames)}")

    scored: list[dict] = []
    failures = 0
    for i, f in enumerate(frames, 1):
        try:
            result = detector.detect(f["path"].read_bytes())
        except Exception as e:  # noqa: BLE001 — report and continue, like the worker
            failures += 1
            print(f"  fail {f['client_id']}: {e}", file=sys.stderr)
            continue
        scored.append({**f, "p": result.probability or 0.0, "n_boxes": len(result.detections)})
        if args.annotate:
            _annotate(f["path"], result.detections, Path(args.annotate), result.probability or 0.0)
        if i % 200 == 0:
            print(f"  ... {i}/{len(frames)}")

    if not scored:
        print("Every frame failed to score.", file=sys.stderr)
        return 1

    probs = [s["p"] for s in scored]
    probs_sorted = sorted(probs)
    print(f"\nScored {len(scored)} frames ({failures} failed)")
    print(f"  mean {sum(probs) / len(probs):.3f}  median {probs_sorted[len(probs) // 2]:.3f}  "
          f"max {max(probs):.3f}")
    print(f"  frames with >=1 box: {sum(1 for s in scored if s['n_boxes'])}")
    _histogram(probs)
    _gray_zone(probs, settings.vlm_verify_low, settings.vlm_verify_high)

    if args.labels:
        _metrics(scored)

    print("\nTop-scoring frames (eyeball these — a top score on a frame with no")
    print("pothole in it is the false-positive mode this phase exists to measure):")
    for s in sorted(scored, key=lambda s: -s["p"])[:10]:
        dev = f"{s['device_probability']:.3f}" if s["device_probability"] is not None else "  -  "
        lab = {1: "pothole", 0: "not", -1: "unsure", None: ""}[s["label"]]
        print(f"  p={s['p']:.3f}  device={dev}  boxes={s['n_boxes']:<2}  {lab:<8} {s['client_id']}")

    if args.annotate:
        print(f"\nAnnotated frames in {args.annotate} (filenames sort by score).")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
