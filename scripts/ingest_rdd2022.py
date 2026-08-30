"""Convert RDD2022 (Pascal-VOC XML) into a YOLO split using Model A's class ids.

Phase 2.7c. Four models in, the detector had never seen a single real-domain pothole.
`yolo11s_pothole_v1..v4` were trained on 3728 archive images that are landscape,
daylight, GoPro, Japan -- while the frames it must score are 480x640 portrait
windshield shots through Toronto rain and at night. Every box this project has
hand-drawn is a NEGATIVE (manhole, grate, patch). So the model's only real-domain
supervision said "road that looks like this is not a pothole", and recall fell with
every labelling session: 0.708 -> 0.431 -> 0.354 -> 0.215. See
docs/phases/phase-2.7b-road-surface-classes.md.

RDD2022 fixes exactly that gap. Its images come from a smartphone mounted on a
vehicle WINDSHIELD -- the same capture modality as this project, which no other
public road-damage set matches -- and it carries real pothole and crack boxes.

LICENCE: CC BY-SA 4.0 (ShareAlike), unlike the CC BY 4.0 archive already in use.
Whether trained weights are an "adaptation" of the images is legally unsettled, so
this ingest is deliberately reversible:

  - the images are never redistributed, only read;
  - models trained with this data must carry `_rdd` in their model id so they can be
    identified and retired;
  - the non-RDD training path keeps working, so a clean-room retrain is always
    possible.

Record the obligation in docs/reference/model-attribution.md. If this is ever sold to a city,
retrain without RDD or get written clarification first.

CLASS REMAP. RDD annotates road *damage*; Model A detects road-surface *objects*.
Only two of its four classes map onto ours:

    D40 Pothole             -> 0 pothole
    D00 Longitudinal crack  -> 4 crack
    D10 Transverse crack    -> 4 crack
    D20 Alligator crack     -> 4 crack
    everything else         -> dropped, and counted

manhole / grate / patch have no RDD equivalent and keep coming from our own labels.
The three crack subtypes collapse into one class because Model A does not report
crack *type* and splitting a data-poor class three ways is how v4 failed.

WHAT IT WRITES: JPEGs and YOLO .txt files under --dest, plus a manifest recording
per-class counts and a SHA-256 of the source archive. It never touches the database
and never modifies the source archive.

Usage (from the repo root):

    python scripts/ingest_rdd2022.py --src <extracted>/United_States --dest <ds>/train --dry-run
    python scripts/ingest_rdd2022.py --src <extracted>/United_States --dest <ds>/train
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252 and the summary below uses an arrow.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.detection.classes import ROAD_SURFACE_CLASSES  # noqa: E402

# RDD damage code -> our class name. Anything absent is dropped and counted, which is
# deliberate: a silent drop is how a dataset quietly stops containing what you think.
_REMAP: dict[str, str] = {
    "D40": "pothole",
    "D00": "crack",
    "D10": "crack",
    "D20": "crack",
}

# Boxes below this fraction of image area are discarded. RDD is annotated for
# pavement assessment and includes hairline cracks a windshield camera cannot
# resolve at speed; training on them teaches "undamaged asphalt is a crack", which
# is the same suppression failure that cost pothole recall in v2/v3.
_MIN_AREA = 0.0002


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _parse_voc(xml_path: Path) -> tuple[int, int, list[tuple[str, float, float, float, float]]]:
    """Pascal-VOC XML -> (width, height, [(code, xmin, ymin, xmax, ymax), ...]).

    Returns pixel coordinates; normalization happens in _to_yolo_line so that the
    single place converting geometry is also the single place that can get it wrong.
    """
    root = ET.parse(xml_path).getroot()
    size = root.find("size")
    width = int(float(size.findtext("width")))
    height = int(float(size.findtext("height")))
    boxes = []
    for obj in root.findall("object"):
        code = (obj.findtext("name") or "").strip()
        bnd = obj.find("bndbox")
        if bnd is None:
            continue
        boxes.append((
            code,
            float(bnd.findtext("xmin")), float(bnd.findtext("ymin")),
            float(bnd.findtext("xmax")), float(bnd.findtext("ymax")),
        ))
    return width, height, boxes


def _to_yolo_line(class_id: int, xmin, ymin, xmax, ymax, width, height) -> str | None:
    """VOC pixel corners -> YOLO normalized centre form. None if degenerate.

    Clamped to the image first: RDD contains boxes that run a pixel or two past the
    edge, and YOLO silently accepts out-of-range coordinates rather than complaining.
    """
    xmin, ymin = max(xmin, 0.0), max(ymin, 0.0)
    xmax, ymax = min(xmax, float(width)), min(ymax, float(height))
    w, h = xmax - xmin, ymax - ymin
    if w <= 1.0 or h <= 1.0:
        return None
    nw, nh = w / width, h / height
    if nw * nh < _MIN_AREA:
        return None
    cx, cy = (xmin + w / 2.0) / width, (ymin + h / 2.0) / height
    if not (0.0 < cx < 1.0 and 0.0 < cy < 1.0):
        return None
    return f"{class_id} {cx:.6f} {cy:.6f} {nw:.6f} {nh:.6f}"


def _find_split(src: Path) -> tuple[Path, Path]:
    """Locate the images and annotations directories inside an RDD country folder.

    The archives nest as <Country>/train/images and <Country>/train/annotations/xmls,
    but the top level is sometimes the country name and sometimes not, so search
    rather than assume and fail loudly with what was actually found.
    """
    for images in sorted(src.rglob("images")):
        if not images.is_dir():
            continue
        for cand in (images.parent / "annotations" / "xmls", images.parent / "annotations"):
            if cand.is_dir() and any(cand.glob("*.xml")):
                return images, cand
    raise SystemExit(
        f"Could not find images/ + annotations/xmls/ under {src}.\n"
        f"Directories seen: " + ", ".join(str(p.relative_to(src)) for p in
                                          sorted(src.rglob('*'))[:20] if p.is_dir())
    )


def main() -> int:
    p = argparse.ArgumentParser(
        description="Ingest an RDD2022 country archive into a YOLO split.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--src", required=True, help="extracted RDD2022 country directory")
    p.add_argument("--dest", required=True,
                   help="YOLO split directory to write into (expects images/ + labels/)")
    p.add_argument("--prefix", default="rdd",
                   help="filename prefix so these can never collide with, or be "
                        "mistaken for, our own frames (default 'rdd')")
    p.add_argument("--archive", help="path to the source .zip, recorded by SHA-256")
    p.add_argument("--manifest", default="runs/rdd2022-manifest.json")
    p.add_argument("--classes", default=",".join(ROAD_SURFACE_CLASSES))
    p.add_argument("--keep-classes",
                   help="comma-separated subset to actually emit; boxes of any other "
                        "class are dropped. Use this to avoid importing a class that "
                        "would dominate the loss -- RDD carries ~10x more crack than "
                        "pothole, and a dominant easy class suppressing a rare hard "
                        "one is exactly how v2/v3/v4 lost recall")
    p.add_argument("--drop-empty", action="store_true",
                   help="skip images left with no boxes. Without it, filtering with "
                        "--keep-classes turns thousands of images into background, "
                        "which is its own suppression problem")
    p.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = p.parse_args()

    classes = [c.strip() for c in args.classes.split(",") if c.strip()]
    class_id = {name: i for i, name in enumerate(classes)}
    keep = None
    if args.keep_classes:
        keep = {c.strip() for c in args.keep_classes.split(",") if c.strip()}
        unknown = keep - set(classes)
        if unknown:
            p.error(f"--keep-classes names {sorted(unknown)}, not in --classes {classes}")

    # Only targets that survive --keep-classes need to exist in --classes. Requiring
    # all of them would force a crack class into a pothole-only run purely because
    # the remap table mentions it.
    needed = set(_REMAP.values()) if keep is None else (set(_REMAP.values()) & keep)
    for target in sorted(needed):
        if target not in class_id:
            p.error(f"remap targets {target!r} which is not in --classes {classes}")

    src, dest = Path(args.src), Path(args.dest)
    images_dir, labels_dir = dest / "images", dest / "labels"
    src_images, src_xmls = _find_split(src)
    print(f"source images     : {src_images}")
    print(f"source annotations: {src_xmls}")

    xmls = sorted(src_xmls.glob("*.xml"))
    if not xmls:
        print(f"No .xml annotations under {src_xmls}.", file=sys.stderr)
        return 1

    kept_boxes: Counter[str] = Counter()
    dropped_codes: Counter[str] = Counter()
    filtered_out: Counter[str] = Counter()
    empty_skipped = 0
    plan: list[tuple[Path, str, list[str]]] = []
    missing_jpeg = 0
    tiny = 0

    for xml_path in xmls:
        width, height, boxes = _parse_voc(xml_path)
        if not width or not height:
            continue
        jpeg = src_images / (xml_path.stem + ".jpg")
        if not jpeg.exists():
            missing_jpeg += 1
            continue
        lines = []
        for code, xmin, ymin, xmax, ymax in boxes:
            name = _REMAP.get(code)
            if name is None:
                dropped_codes[code or "(empty)"] += 1
                continue
            if keep is not None and name not in keep:
                filtered_out[name] += 1
                continue
            line = _to_yolo_line(class_id[name], xmin, ymin, xmax, ymax, width, height)
            if line is None:
                tiny += 1
                continue
            lines.append(line)
            kept_boxes[name] += 1
        if not lines and args.drop_empty:
            empty_skipped += 1
            continue
        plan.append((jpeg, f"{args.prefix}-{xml_path.stem}", lines))

    positives = sum(1 for _, _, lines in plan if lines)
    backgrounds = len(plan) - positives

    print(f"\n{len(xmls)} annotation file(s)")
    print(f"images to write   : {len(plan)}  ({positives} with boxes, "
          f"{backgrounds} background)")
    print("boxes kept, by class:")
    for name in classes:
        print(f"  {class_id[name]} {name:<9} {kept_boxes.get(name, 0)}")
    if filtered_out:
        print("boxes excluded by --keep-classes:")
        for name, n in filtered_out.most_common():
            print(f"  {name:<9} {n}")
    if empty_skipped:
        print(f"{empty_skipped} image(s) skipped as empty after filtering (--drop-empty)")
    if dropped_codes:
        print("RDD codes dropped (no Model A equivalent):")
        for code, n in dropped_codes.most_common():
            print(f"  {code:<9} {n}")
    if tiny:
        print(f"{tiny} box(es) dropped as too small or degenerate "
              f"(< {_MIN_AREA:.4%} of frame area)")
    if missing_jpeg:
        print(f"{missing_jpeg} annotation(s) had no matching .jpg")

    if not kept_boxes:
        print("\nNothing maps onto Model A's classes -- refusing to write an "
              "all-background split.", file=sys.stderr)
        return 1

    if args.dry_run:
        print("\n--dry-run: nothing written.")
        return 0

    images_dir.mkdir(parents=True, exist_ok=True)
    labels_dir.mkdir(parents=True, exist_ok=True)

    import shutil
    for jpeg, stem, lines in plan:
        # Copied, not re-encoded: a second JPEG generation would add compression
        # artefacts to data the model is meant to learn fine texture from.
        shutil.copy2(jpeg, images_dir / f"{stem}.jpg")
        (labels_dir / f"{stem}.txt").write_text(
            "".join(line + "\n" for line in lines), encoding="utf-8")

    manifest = {
        "generated_at": datetime.now(UTC).isoformat(),
        "source": str(src),
        "dataset": "RDD2022",
        "licence": "CC BY-SA 4.0",
        "licence_note": (
            "ShareAlike. Weights trained on this data carry '_rdd' in their model id "
            "so they can be identified and retired. Images are never redistributed."
        ),
        "archive_sha256": _sha256(Path(args.archive)) if args.archive else None,
        "prefix": args.prefix,
        "classes": classes,
        "remap": _REMAP,
        "images": len(plan),
        "with_boxes": positives,
        "backgrounds": backgrounds,
        "box_counts": dict(kept_boxes),
        "keep_classes": sorted(keep) if keep else None,
        "filtered_out_boxes": dict(filtered_out),
        "empty_images_skipped": empty_skipped,
        "dropped_codes": dict(dropped_codes),
        "dropped_small_boxes": tiny,
    }
    Path(args.manifest).parent.mkdir(parents=True, exist_ok=True)
    Path(args.manifest).write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    print(f"\nWrote {len(plan)} image(s) and label file(s) to {dest}")
    print(f"manifest -> {args.manifest}")
    print("\nDelete any stale labels.cache in the dataset before training -- YOLO will "
          "otherwise train on the previous file list.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
