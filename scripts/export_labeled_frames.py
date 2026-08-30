"""Export hand-labelled real frames into a YOLO dataset split.

Phase 2.7b (was `export_negatives.py`, Phase 2.7). The old name described what this
could do when `frame_label` was all there was: mine `label = 0` rows as background
images. That turned out to be the problem rather than the fix.

WHAT WENT WRONG WITH BACKGROUND-ONLY EXPORT. `frame_label` records a verdict per
frame, not a box, so the pothole-labelled frames carried no coordinates and could not
train anything. Every labelling session therefore added negatives and only negatives
-- and because the labelling queue is stratified toward frames the detector already
found interesting, those negatives are disproportionately manholes, tar seals and
grates: dark, roughly pothole-shaped, sitting on road surface. With a single class the
model's only way to explain one is "background", which it achieves by suppressing
dark-irregular-shape-on-asphalt. Real potholes are dark irregular shapes on asphalt.
Measured over three models: recall 0.708 -> 0.431 -> 0.354 as more labels arrived.

WHAT THIS DOES NOW. `frame_box` (migration 013) holds human boxes, so a frame can
carry manhole/grate/patch annotations and the model gets somewhere to put them other
than "background". This writes:

  - a `.txt` line per box, converted corner-origin -> YOLO centre-origin HERE and
    nowhere else (see `_to_yolo`),
  - an EMPTY `.txt` for a frame reviewed and found genuinely clean,
  - a `data.yaml` names: block from the same class list the decoder uses.

REVIEWED IS NOT THE SAME AS EMPTY. A frame with `boxed_at IS NULL` has never been
opened, so its lack of boxes means nothing; exporting it as background is precisely
the mistake above. Those frames are skipped and counted. Only `boxed_at IS NOT NULL`
becomes training data.

HOLDOUT: training on a frame and then evaluating on it manufactures an improvement out
of leakage. Frames are split deterministically (md5 of client_id, so re-running is
stable and independent of row order) into a training share and a holdout. Only the
training share is written; the manifest records both, and
`scripts/detect_eval.py --exclude-ids` consumes the training list.

It never writes to the database and never touches the source frames.

Usage (from the repo root):

    python scripts/export_labeled_frames.py --dest <dataset>/train --dry-run
    python scripts/export_labeled_frames.py --dest <dataset>/train --no-roi
    python scripts/export_labeled_frames.py --dest <d>/train --ids <ids>.txt
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import sys
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252 and the summary below uses an arrow.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image  # noqa: E402

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402
from app.detection.classes import ROAD_SURFACE_CLASSES  # noqa: E402
from app.services.frame_service import resolve_local_frame_path  # noqa: E402

# Only reviewed frames. `boxed_at IS NULL` means nobody has looked for boxes, so an
# absence of rows in frame_box says nothing at all about the image.
_FRAMES_SQL = """
SELECT l.frame_client_id, l.label, f.device_id, f.jpeg_url,
       COALESCE((
           SELECT json_agg(json_build_object(
                      'class_id', b.class_id, 'x', b.x, 'y', b.y, 'w', b.w, 'h', b.h)
                  ORDER BY b.id)
           FROM frame_box b WHERE b.frame_client_id = l.frame_client_id
       ), '[]'::json)::text AS boxes
FROM frame_label l
JOIN asset_frame f ON f.client_id = l.frame_client_id
WHERE l.boxed_at IS NOT NULL
ORDER BY l.frame_client_id
"""

# Counted and reported, never exported. This is the number that says how much of a
# labelling session actually became training data.
_UNREVIEWED_SQL = """
SELECT count(*) FROM frame_label WHERE label IN (0, 1) AND boxed_at IS NULL
"""


def _is_train(client_id: str, train_share: float) -> bool:
    """Stable train/holdout assignment.

    md5 rather than hash(): Python's string hash is salted per process, so hash()
    would reshuffle the split on every run and silently leak holdout frames into
    training on the second pass.
    """
    digest = hashlib.md5(client_id.encode()).hexdigest()
    return (int(digest[:8], 16) / 0xFFFFFFFF) < train_share


def _to_yolo(box: dict) -> tuple[int, float, float, float, float]:
    """Corner-origin (x, y, w, h) -> YOLO centre-origin (class, cx, cy, w, h).

    THE ONLY PLACE THIS CONVERSION HAPPENS. `frame_box`, `device_detections` and
    `server_detections` all store corner-origin normalized 0..1; YOLO's .txt format
    wants the centre. Doing it anywhere else means two conventions in one codebase,
    and a swapped one does not crash -- it trains a model on boxes offset by half
    their own size, which looks like a mediocre model rather than a bug.
    """
    return (
        int(box["class_id"]),
        box["x"] + box["w"] / 2.0,
        box["y"] + box["h"] / 2.0,
        box["w"],
        box["h"],
    )


def _from_yolo(class_id: int, cx: float, cy: float, w: float, h: float) -> dict:
    """Inverse of `_to_yolo`. Exists so the round trip can be asserted in a test."""
    return {"class_id": class_id, "x": cx - w / 2.0, "y": cy - h / 2.0, "w": w, "h": h}


def _label_text(boxes: list[dict]) -> str:
    """YOLO label file contents. Empty string = "this image contains nothing"."""
    return "".join(
        "{} {:.6f} {:.6f} {:.6f} {:.6f}\n".format(*_to_yolo(b)) for b in boxes
    )


def _write_data_yaml(dest: Path, classes: list[str]) -> Path:
    """Write names: from the same list the decoder reads.

    A dataset whose class order disagrees with DETECTION_CLASS_NAMES produces a model
    that silently scores the wrong class into server_probability -- and fusion cannot
    detect that, because it never sees a class at all.
    """
    root = dest.parent
    path = root / "data.yaml"
    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(classes))
    path.write_text(
        f"# Written by scripts/export_labeled_frames.py -- do not hand-edit `names`.\n"
        f"# Order must match app/detection/classes.py and DETECTION_CLASS_NAMES.\n"
        f"path: {root.resolve().as_posix()}\n"
        f"train: train/images\n"
        f"val: valid/images\n"
        f"test: test/images\n"
        f"nc: {len(classes)}\n"
        f"names:\n{names}\n",
        encoding="utf-8",
    )
    return path


def _roi_crop(img: Image.Image) -> Image.Image:
    """Crop to the detector's ROI band, matching app/detection/onnx_v1.py._roi.

    The model is fed an ROI crop at inference, so a background it trains on should
    look like one. Full width is always kept; only the height band is cut.

    MEASURED AND NOT RECOMMENDED -- prefer --no-roi. The archive positives are full
    images, so cropping only the negatives makes crop geometry a perfect predictor of
    class and the model learns "ROI-shaped input => background" instead of learning
    what road looks like. yolo11s_pothole_v2 was trained this way: false positives fell
    69% as intended, but 39% of true positives went too, and the ROI-on/ROI-off
    preference reversed relative to v1 -- while archive-split metrics stayed put,
    localising the damage to real cropped frames. See
    docs/phases/phase-2.7-detection-enablement.md. The flag is kept because it becomes correct
    the moment positives are cropped the same way.
    """
    w, h = img.size
    y0 = int(round(h * settings.detection_roi_top))
    y1 = int(round(h * settings.detection_roi_bottom))
    if y1 - y0 < 16:  # degenerate band: fall back to the whole frame
        return img
    return img.crop((0, y0, w, y1))


async def main_async(args) -> int:
    dest = Path(args.dest)
    images_dir, labels_dir = dest / "images", dest / "labels"
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    only = None
    if args.ids:
        only = {
            line.strip()
            for line in Path(args.ids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    pool = await create_pool()
    try:
        rows = await pool.fetch(_FRAMES_SQL)
        unreviewed = await pool.fetchval(_UNREVIEWED_SQL)
    finally:
        await pool.close()

    if only is not None:
        rows = [r for r in rows if r["frame_client_id"] in only]

    if not rows:
        print("No frames have been reviewed for boxes. Nothing to export.")
        print("  Review them with: python scripts/label_frames.py --box "
              "--ids runs/negatives-train-ids.txt")
        if unreviewed:
            print(f"  ({unreviewed} judged frame(s) are waiting for a box review.)")
        return 1

    parsed = [{**dict(r), "box_list": json.loads(r["boxes"] or "[]")} for r in rows]
    train, holdout = [], []
    for r in parsed:
        (train if _is_train(r["frame_client_id"], args.train_share) else holdout).append(r)

    counts = Counter(
        b["class_id"] for r in train for b in r["box_list"]
    )
    backgrounds = sum(1 for r in train if not r["box_list"])

    print(f"{len(rows)} reviewed frame(s): {len(train)} to train, {len(holdout)} held out "
          f"(train_share={args.train_share:g})")
    if only is not None:
        print(f"  restricted to {len(only)} id(s) from {args.ids}")
    if unreviewed:
        # The single most useful number here: how much of the labelled set is still
        # invisible to training because nobody has drawn on it.
        print(f"  {unreviewed} judged frame(s) NOT reviewed for boxes -- excluded. "
              f"An unreviewed frame is not a background image.")
    print(f"dest: {images_dir}")
    print("boxes in the training share:")
    for idx, name in enumerate(classes):
        print(f"  {idx} {name:<10} {counts.get(idx, 0)}")
    print(f"  {'(background)':<13} {backgrounds} frame(s) with no boxes")
    if args.roi:
        print(f"crop: ROI {settings.detection_roi_top:.2f}-{settings.detection_roi_bottom:.2f}")
    else:
        print("crop: none (full frame)")

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    written, missing, boxes_written = 0, 0, 0
    for r in train:
        # Same resolver detect_eval.py uses, so the two agree on where a frame lives.
        try:
            src = resolve_local_frame_path(r["jpeg_url"])
        except Exception as e:  # noqa: BLE001 -- a bad path is data, not a crash
            print(f"  skip {r['frame_client_id']}: {e}", file=sys.stderr)
            missing += 1
            continue
        if not src.exists():
            missing += 1
            continue
        # Prefix so a real frame can never collide with an archive filename, and so
        # `del real-*` is enough to undo this.
        stem = f"real-{r['frame_client_id']}"
        with Image.open(src) as im:
            im = im.convert("RGB")
            (_roi_crop(im) if args.roi else im).save(images_dir / f"{stem}.jpg", quality=92)
        # An empty file is how YOLO spells "this image contains nothing" -- which is
        # only truthful because _FRAMES_SQL filtered to reviewed frames.
        (labels_dir / f"{stem}.txt").write_text(
            _label_text(r["box_list"]), encoding="utf-8"
        )
        boxes_written += len(r["box_list"])
        written += 1

    yaml_path = _write_data_yaml(dest, classes)

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "train_share": args.train_share,
        "roi_cropped": bool(args.roi),
        "roi": [settings.detection_roi_top, settings.detection_roi_bottom] if args.roi else None,
        "dest": str(dest),
        "classes": classes,
        "class_counts": {classes[k]: v for k, v in sorted(counts.items()) if k < len(classes)},
        "backgrounds": backgrounds,
        "unreviewed_excluded": unreviewed,
        "train_ids": [r["frame_client_id"] for r in train],
        "holdout_ids": [r["frame_client_id"] for r in holdout],
    }
    Path(args.manifest).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    # The plain id list is what detect_eval.py --exclude-ids reads.
    Path(args.exclude_out).write_text(
        "\n".join(r["frame_client_id"] for r in train) + "\n", encoding="utf-8"
    )

    print(f"\nWrote {written} image(s) carrying {boxes_written} box(es)"
          + (f"; {missing} JPEG(s) missing on disk" if missing else ""))
    print(f"data.yaml     -> {yaml_path}")
    print(f"manifest      -> {args.manifest}")
    print(f"exclude list  -> {args.exclude_out}")
    print("\nEvaluate the retrained model on the holdout only:")
    print(f"  python scripts/detect_eval.py --model <new>.onnx --labels "
          f"--exclude-ids {args.exclude_out} --classes {args.classes}")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Export hand-labelled real frames as a YOLO dataset split.",
    )
    p.add_argument("--dest", required=True,
                   help="YOLO split directory to write into (expects images/ + labels/)")
    p.add_argument("--train-share", type=float, default=0.7,
                   help="fraction used for training; the rest is held out (default 0.7)")
    p.add_argument("--manifest", default="runs/negatives-manifest.json",
                   help="where to record the split (default runs/negatives-manifest.json)")
    p.add_argument("--exclude-out", default="runs/negatives-train-ids.txt",
                   help="plain id list for detect_eval.py --exclude-ids")
    p.add_argument("--roi", action=argparse.BooleanOptionalAction, default=True,
                   help="crop to the detector's ROI band (default: on)")
    p.add_argument("--ids", help="file of client_ids, one per line: export only these")
    p.add_argument("--classes", default=",".join(ROAD_SURFACE_CLASSES),
                   help="comma-separated class names; position is the class_id")
    p.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = p.parse_args()
    if not 0.0 < args.train_share < 1.0:
        p.error("--train-share must be strictly between 0 and 1")
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
