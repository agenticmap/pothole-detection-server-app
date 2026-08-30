"""Decide whether a candidate detector may replace the incumbent. Exits non-zero if not.

Phase 2.7c. Five models have been trained. Four of them were worse than the one they
were meant to improve on, and every one of them looked fine on the metric that was
easy to read:

    model  archive mAP50   real-frame recall
    v1         0.512            0.708
    v2         ~0.51            0.431
    v3         0.494            0.354
    v4         0.513            0.215
    v5         0.524            0.677

Archive mAP50 sat in a 0.03 band across models ranging from the best to the worst.
It has never once predicted quality on real frames. So "the numbers looked good" is
not a safe promotion criterion here, and this script exists to replace it.

WHAT IT CHECKS. Both models are scored over the same hand-labelled holdout, and
compared at MATCHED RECALL rather than at a matched threshold. Two models rarely
share a calibration -- v1's best F1 is at 0.05 and v3's at 0.05 but with a third of
the recall -- so comparing them at the same threshold compares two arbitrary
operating points. Matched recall asks the only question that matters: at the same
number of real potholes found, which model pays fewer false positives?

TWO OBJECTIVES, BECAUSE THERE ARE TWO JOBS. A single "must win everywhere" rule
deadlocks on this repo's own models: v1 reaches 46/65 true positives to v3's 23, yet
v3 pays fewer false positives at every point they share. Neither could ever replace
the other. That is not a tie -- they are good at different things.

  --objective fusion (default)
      What ships as DETECTION_MODEL_PATH. Fusion consumes the visual term as a
      modifier on a sensor verdict, and a silent detector falls back to
      device_probability -- i.e. today's behaviour -- so a detector that rarely
      speaks but is right when it does beats a noisy one. REQUIRES no regression at
      any matched true-positive count. A lower ceiling is reported, not fatal.

  --objective sampler
      What ranks frames for a human review queue. Reach is everything here: a frame
      the model never fires on never enters the queue, and a false positive costs
      review time, which is what the queue is for. REQUIRES the recall ceiling not to
      drop. Extra false positives are reported, not fatal.

F1 is used for neither. It rewards the opposite trade from fusion, and by F1 v1
outscores v3 while being the worse model to ship.

WHAT IT WRITES: nothing. It reads frames and prints a verdict.

Usage (from the repo root):

    python scripts/promote_model.py --candidate runs/new/weights/best.onnx \\
           --incumbent runs/pothole_v1/weights/best.onnx \\
           --exclude-ids runs/negatives-train-ids.txt
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from app.database import create_pool  # noqa: E402
from app.detection.classes import ROAD_SURFACE_CLASSES  # noqa: E402
from app.detection.onnx_v1 import OnnxYoloDetector  # noqa: E402
from app.services.frame_service import resolve_local_frame_path  # noqa: E402

_LABELLED_SQL = """
SELECT l.frame_client_id, l.label, f.jpeg_url
FROM frame_label l
JOIN asset_frame f ON f.client_id = l.frame_client_id
WHERE l.label IN (0, 1)
ORDER BY l.frame_client_id
"""

# Match detect_eval.py's sweep so the two tools cannot disagree about a threshold.
_THRESHOLDS = [i / 20 for i in range(1, 20)]


def _curve(scored: list[tuple[int, float]]) -> dict[int, tuple[float, int]]:
    """{true positives caught: (threshold, false positives)} -- the matched-recall curve.

    Keyed by TP because that is the axis being matched. Where several thresholds give
    the same TP, the one with the fewest FP wins: that is the best this model can do
    at that recall, which is what it should be judged on.
    """
    out: dict[int, tuple[float, int]] = {}
    for t in _THRESHOLDS:
        tp = sum(1 for lb, p in scored if p >= t and lb == 1)
        fp = sum(1 for lb, p in scored if p >= t and lb == 0)
        if tp and (tp not in out or fp < out[tp][1]):
            out[tp] = (t, fp)
    return out


def _build(path: str, args) -> OnnxYoloDetector:
    return OnnxYoloDetector(
        model_path=path,
        model_id=Path(path).stem,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        input_size=args.input_size,
        roi_enabled=args.roi,
        roi_top=args.roi_top,
        roi_bottom=args.roi_bottom,
        labels=[c.strip() for c in args.classes.split(",") if c.strip()],
        primary_class_id=args.primary_class,
    )


async def _score(detector: OnnxYoloDetector, rows, excluded: set[str]) -> list[tuple[int, float]]:
    out = []
    for r in rows:
        if r["frame_client_id"] in excluded:
            continue
        try:
            path = resolve_local_frame_path(r["jpeg_url"])
        except Exception:  # noqa: BLE001 -- a bad path is a skipped frame, not a crash
            continue
        if not path.exists():
            continue
        out.append((r["label"], detector.detect(path.read_bytes()).probability))
    return out


async def main_async(args) -> int:
    pool = await create_pool()
    try:
        rows = await pool.fetch(_LABELLED_SQL)
    finally:
        await pool.close()

    excluded: set[str] = set()
    if args.exclude_ids:
        excluded = {
            line.strip()
            for line in Path(args.exclude_ids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }

    print(f"candidate : {args.candidate}")
    print(f"incumbent : {args.incumbent}")
    if excluded:
        print(f"excluding {len(excluded)} training frame(s) from {args.exclude_ids}")

    cand = await _score(_build(args.candidate, args), rows, excluded)
    inc = await _score(_build(args.incumbent, args), rows, excluded)
    positives = sum(1 for lb, _ in cand if lb == 1)
    print(f"holdout   : {len(cand)} frames ({positives} pothole, {len(cand)-positives} not)\n")
    if positives < args.min_positives:
        print(f"REFUSING TO JUDGE: {positives} labelled positives is below --min-positives "
              f"({args.min_positives}). A holdout this small cannot separate two models; "
              f"label more frames before promoting anything.", file=sys.stderr)
        return 2

    c_curve, i_curve = _curve(cand), _curve(inc)
    shared = sorted(set(c_curve) & set(i_curve), reverse=True)

    print(f"{'TP':>4} {'recall':>7} | {'inc thr':>8} {'inc FP':>7} | "
          f"{'cand thr':>9} {'cand FP':>8} | verdict")
    regressions = 0
    for tp in shared:
        i_thr, i_fp = i_curve[tp]
        c_thr, c_fp = c_curve[tp]
        if c_fp < i_fp:
            verdict = "better"
        elif c_fp == i_fp:
            verdict = "same"
        else:
            verdict = "WORSE"
            regressions += 1
        print(f"{tp:>4} {tp/positives:>7.3f} | {i_thr:>8.2f} {i_fp:>7} | "
              f"{c_thr:>9.2f} {c_fp:>8} | {verdict}")

    c_ceil, i_ceil = (max(c_curve) if c_curve else 0), (max(i_curve) if i_curve else 0)
    print(f"\nrecall ceiling: incumbent {i_ceil}/{positives} = {i_ceil/positives:.3f}   "
          f"candidate {c_ceil}/{positives} = {c_ceil/positives:.3f}")
    print(f"objective     : {args.objective}")

    failed, noted = [], []
    if not shared:
        failed.append("the two models share no operating point, so they cannot be compared")
    if args.objective == "fusion":
        if regressions:
            failed.append(f"{regressions} matched-recall point(s) pay more false positives")
        if c_ceil < i_ceil:
            noted.append(f"recall ceiling drops {i_ceil} -> {c_ceil} TP, which this objective "
                         f"tolerates: a detector that speaks less often but more accurately "
                         f"still helps fusion")
    else:
        if c_ceil < i_ceil:
            failed.append(f"recall ceiling dropped {i_ceil} -> {c_ceil} true positives")
        if regressions:
            noted.append(f"{regressions} point(s) pay more false positives, which this "
                         f"objective tolerates: queue precision costs review time, not "
                         f"correctness")

    for n in noted:
        print(f"  note: {n}")

    if failed:
        print("\nREJECTED. " + "; ".join(failed) + ".")
        print("The incumbent stays. Nothing was written.")
        return 1

    print(f"\nPROMOTE for objective '{args.objective}'.")
    print("  Record the run, licence and SHA-256 in docs/reference/model-attribution.md.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Gate a candidate detector against the incumbent on the labelled holdout.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("--candidate", required=True, help="the new .onnx")
    p.add_argument("--incumbent", required=True, help="the model it would replace")
    p.add_argument("--exclude-ids",
                   help="file of client_ids to skip, one per line -- the candidate's "
                        "training frames, so it cannot be scored on its own data")
    p.add_argument("--objective", choices=("fusion", "sampler"), default="fusion",
                   help="fusion: what ships to DETECTION_MODEL_PATH, judged on false "
                        "positives at matched recall. sampler: what ranks a review "
                        "queue, judged on recall ceiling. See the module docstring")
    p.add_argument("--min-positives", type=int, default=30,
                   help="refuse to judge on fewer labelled positives than this (default 30)")
    p.add_argument("--conf", type=float, default=0.05,
                   help="detector floor. 0.05, not 0.25: this detector's useful range is "
                        "low and a high floor records zeros for real positives")
    p.add_argument("--iou", type=float, default=0.45)
    p.add_argument("--input-size", type=int, default=640)
    p.add_argument("--classes", default=",".join(ROAD_SURFACE_CLASSES[:1]))
    p.add_argument("--primary-class", type=int, default=0)
    roi = p.add_mutually_exclusive_group()
    roi.add_argument("--roi", dest="roi", action="store_true",
                     help="crop to the road band (default)")
    roi.add_argument("--no-roi", dest="roi", action="store_false")
    p.set_defaults(roi=True)
    p.add_argument("--roi-top", type=float, default=0.45)
    p.add_argument("--roi-bottom", type=float, default=0.90)
    args = p.parse_args()
    for path in (args.candidate, args.incumbent):
        if not Path(path).exists():
            print(f"Model not found: {path}", file=sys.stderr)
            return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
