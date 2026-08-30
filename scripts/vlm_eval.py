"""Measure a VLM verifier against human labels. Writes nothing to the database.

Phase 2.9. app/detection/hybrid_v1.py has shipped since Phase 2.3b with 21 tests --
every one of them against a *fake* verifier. No VLM has ever seen a frame from this
corpus, so every number behind the hybrid design is an assumption:

  - "~97% accuracy, <2% FP" in docs/architecture/detection-approach.md is from the literature.
  - VLM_VERIFY_LOW/HIGH (0.40/0.75) were picked as a cost bound before a detector
    existed. Measured against the 340 labelled frames, "auto-accept >0.75" fires on
    5 frames of 5615 and the one labelled frame up there is a FALSE POSITIVE, while
    "auto-drop <0.40" would discard 55 of the 65 known potholes.

This script produces the missing measurement. It scores every labelled frame through
the REAL production path -- HybridDetector with the gray zone opened to [0,1], so the
crop, the blend and the fallback are the ones that would actually run -- and reports:

  1. The VLM's binary verdict vs ground truth (confusion matrix). This is the number
     that decides whether pseudo-labelling is admissible at all. Note that pseudo-
     labelled NEGATIVES are the exact mechanism that took recall 0.708 -> 0.215
     across v2/v3/v4, so the bar for them is high.
  2. Matched-recall curves for Stage 1 alone / the VLM alone / the blend, on the same
     axis promote_model.py uses, so this is comparable to v1/v3/v5.
  3. Score-band tables, which is what calibrates VLM_VERIFY_LOW/HIGH.

NEVER WRITES TO THE DATABASE, and takes its config from CLI flags rather than
DETECTION_*/VLM_* env vars, so a run cannot silently measure a different model than
the flag says. Same contract as scripts/detect_eval.py.

Usage (from the repo root):

    # Free end-to-end smoke against a local model first -- no key, no cost.
    ollama pull qwen2.5vl:7b
    python scripts/vlm_eval.py --model runs/pothole_v1/weights/best.onnx \\
           --backend ollama --vlm-model qwen2.5vl:7b --limit 5

    # Then a real sweep. --cache means every later analysis is free.
    python scripts/vlm_eval.py --model runs/pothole_v1/weights/best.onnx \\
           --backend openrouter --vlm-model qwen/qwen2.5-vl-72b-instruct \\
           --limit 0 --cache runs/vlm-openrouter-qwen72b.json

    # Re-analyse a finished cache: sweep the blend weight, no API calls, no model.
    python scripts/vlm_eval.py --analyse-only --cache runs/vlm-openrouter-qwen72b.json

PRIVACY: a cloud backend uploads road imagery to a third party. Frames are
EXIF-stripped on ingest, but pixels can hold plates and faces. Prefer ollama for
anything beyond research. See docs/phases/phase-2.9-vlm-verification.md.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(_SCRIPTS.parent))
sys.path.insert(0, str(_SCRIPTS))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from promote_model import _curve  # noqa: E402 -- one definition of the matched-recall curve

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402
from app.detection.classes import ROAD_SURFACE_CLASSES  # noqa: E402
from app.detection.hybrid_v1 import HybridDetector  # noqa: E402
from app.detection.onnx_v1 import OnnxYoloDetector  # noqa: E402
from app.detection.vlm.base import VlmVerdict  # noqa: E402
from app.detection.vlm.registry import get_verifier  # noqa: E402
from app.services.frame_service import resolve_local_frame_path  # noqa: E402

# Only frames a human has actually judged. label = -1 ("cannot tell") is excluded:
# it is a recorded decision, not ground truth, and cannot score a model.
_LABELLED_SQL = """
SELECT l.frame_client_id AS client_id, l.label, f.jpeg_url, f.server_probability
FROM frame_label l
JOIN asset_frame f ON f.client_id = l.frame_client_id
WHERE l.label IN (0, 1)
ORDER BY l.frame_client_id
"""

_BANDS = [(0.00, 0.05), (0.05, 0.15), (0.15, 0.30), (0.30, 0.40), (0.40, 0.75), (0.75, 1.01)]


# ── running ────────────────────────────────────────────────────────────────────

def _vlm_probability(rec: dict) -> float | None:
    """The verdict as a pothole-probability, matching HybridDetector._blend.

    A confident "not a pothole" is evidence AGAINST, so it maps low, not to 0.5.
    """
    if rec.get("is_pothole") is None:
        return None
    return rec["confidence"] if rec["is_pothole"] else 1.0 - rec["confidence"]


def _score_frames(rows, hybrid: HybridDetector, cache: dict, cache_path: Path | None) -> dict:
    """Run every row through the hybrid, reusing cached verdicts. Returns the cache.

    Cached frames make no API call, so a crashed or interrupted sweep resumes instead
    of re-paying for work already done. The cache is flushed every 10 new frames for
    the same reason.
    """
    fresh = 0
    for i, r in enumerate(rows, 1):
        cid = r["client_id"]
        if cid in cache:
            continue
        try:
            path = resolve_local_frame_path(r["jpeg_url"])
        except Exception as e:  # noqa: BLE001 -- a bad path is a skipped frame, not a crash
            print(f"  skip {cid}: {e}", file=sys.stderr)
            continue
        if not path.exists():
            print(f"  skip {cid}: file missing", file=sys.stderr)
            continue

        # HybridDetector.detect() is unrolled here rather than called, for one reason:
        # it returns only the blended probability, so recovering p1 would mean a second
        # ONNX pass over every frame. The steps and the methods are its own -- same
        # crop, same blend, same order -- so this measures the production path.
        jpeg = path.read_bytes()
        r1 = hybrid.stage1.detect(jpeg)
        p1 = r1.probability
        verdict = None
        if p1 is not None:
            image = hybrid._crop(jpeg, r1.detections) if hybrid.crop else jpeg
            try:
                verdict = hybrid.verifier.verify(image, {"stage1_p": p1})
            except Exception as e:  # noqa: BLE001 -- hybrid_v1 falls back to Stage 1 too
                print(f"  verify failed for {cid}: {e}", file=sys.stderr)

        rec = {
            "client_id": cid,
            "label": r["label"],
            "p1": p1,
            "p_blend": hybrid._blend(p1, verdict) if verdict else p1,
            "boxed": any(isinstance(d.get("bbox"), dict) for d in r1.detections),
            "is_pothole": verdict.is_pothole if verdict else None,
            "confidence": verdict.confidence if verdict else None,
            "severity": verdict.severity if verdict else None,
            "rationale": verdict.rationale if verdict else None,
        }
        cache[cid] = rec
        fresh += 1
        flag = "?" if verdict is None else ("POTHOLE" if rec["is_pothole"] else "no")
        print(f"  [{i}/{len(rows)}] {cid} label={r['label']} "
              f"p1={'--' if p1 is None else format(p1, '.3f')} vlm={flag} "
              f"conf={rec['confidence']} -> "
              f"{'--' if rec['p_blend'] is None else format(rec['p_blend'], '.3f')}")
        if cache_path and fresh % 10 == 0:
            _save_cache(cache_path, cache)
    if cache_path and fresh:
        _save_cache(cache_path, cache)
    return cache


def _save_cache(path: Path, cache: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")


# ── reporting ──────────────────────────────────────────────────────────────────

def _confusion(records: list[dict]) -> None:
    """The VLM's own binary verdict against ground truth -- no threshold involved.

    This is the number pseudo-labelling would rest on: a false-positive rate here is
    a wrong training box, and a false-negative rate here is a discarded positive from
    the very pool the detector is short of.
    """
    judged = [r for r in records if r["is_pothole"] is not None]
    print(f"\n── VLM binary verdict ({len(judged)} frames answered, "
          f"{len(records) - len(judged)} unanswered) ──")
    if not judged:
        print("  no verdicts -- every call failed or fell back to Stage 1.")
        return
    tp = sum(1 for r in judged if r["is_pothole"] and r["label"] == 1)
    fp = sum(1 for r in judged if r["is_pothole"] and r["label"] == 0)
    fn = sum(1 for r in judged if not r["is_pothole"] and r["label"] == 1)
    tn = sum(1 for r in judged if not r["is_pothole"] and r["label"] == 0)
    prec = tp / (tp + fp) if tp + fp else 0.0
    rec = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
    pos = tp + fn
    print("           said pothole   said not")
    print(f"  IS      {tp:>12} {fn:>10}")
    print(f"  IS NOT  {fp:>12} {tn:>10}")
    print(f"  precision {prec:.3f}  recall {rec:.3f}  F1 {f1:.3f}  "
          f"accuracy {(tp + tn) / len(judged):.3f}")
    print(f"  base rate {pos / len(judged):.3f} -- precision below this means the "
          f"verdict is worse than guessing 'pothole' every time.")


def _curve_table(title: str, scored: list[tuple[int, float]], positives: int) -> None:
    curve = _curve(scored)
    print(f"\n── {title} ──")
    if not curve:
        print("  no operating point produces a true positive.")
        return
    print(f"  {'TP':>4} {'recall':>7} {'thr':>6} {'FP':>5} {'precision':>10}")
    for tp in sorted(curve, reverse=True):
        thr, fp = curve[tp]
        print(f"  {tp:>4} {tp / positives:>7.3f} {thr:>6.2f} {fp:>5} "
              f"{tp / (tp + fp):>10.3f}")
    print(f"  ceiling {max(curve)}/{positives} = {max(curve) / positives:.3f}")


def _band_table(title: str, scored: list[tuple[int, float]]) -> None:
    print(f"\n── {title} ──")
    total_pos = sum(1 for lb, _ in scored if lb == 1)
    base = total_pos / len(scored) if scored else 0.0
    print(f"  {'band':>12} {'frames':>7} {'pothole':>8} {'rate':>7} {'lift':>6}")
    for lo, hi in _BANDS:
        sel = [lb for lb, p in scored if lo <= p < hi]
        if not sel:
            continue
        pos = sum(1 for lb in sel if lb == 1)
        rate = pos / len(sel)
        print(f"  {lo:.2f}-{hi:.2f}".rjust(14)
              + f" {len(sel):>7} {pos:>8} {rate:>7.3f} "
              + f"{(rate / base if base else 0):>6.2f}x")
    print(f"  base rate {base:.3f} over {len(scored)} frames "
          f"({total_pos} pothole)")


def _report(records: list[dict], blend_weights: list[float]) -> None:
    records = [r for r in records if r["p1"] is not None]
    positives = sum(1 for r in records if r["label"] == 1)
    boxed = sum(1 for r in records if r["boxed"])
    print(f"\n{'=' * 74}")
    print(f"{len(records)} labelled frames scored -- {positives} pothole, "
          f"{len(records) - positives} not")
    print(f"Stage 1 produced a box on {boxed} of them; the other "
          f"{len(records) - boxed} were sent to the VLM as FULL FRAMES "
          f"(nothing to crop to), so the crop ablation only bites on the {boxed}.")
    print("=" * 74)

    if positives == 0:
        print("\nNo positives -- nothing can be measured. Label some frames first.")
        return

    _confusion(records)

    s1 = [(r["label"], r["p1"]) for r in records]
    vlm = [(r["label"], _vlm_probability(r)) for r in records]
    vlm = [(lb, p) for lb, p in vlm if p is not None]
    blend = [(r["label"], r["p_blend"]) for r in records]

    _curve_table("Stage 1 alone (matched-recall)", s1, positives)
    _curve_table("VLM alone (matched-recall)", vlm, sum(1 for lb, _ in vlm if lb == 1))
    _curve_table("Blended, as configured (matched-recall)", blend, positives)

    _band_table("Stage-1 score bands", s1)
    _band_table("Blended score bands -- THIS is what calibrates VLM_VERIFY_LOW/HIGH", blend)

    # Re-blending is pure arithmetic on cached values, so sweeping costs nothing.
    if blend_weights:
        print("\n── Blend-weight sweep (no API calls; recomputed from the cache) ──")
        print(f"  {'w':>5} {'ceiling TP':>11} {'best F1':>9} {'at thr':>7}")
        scratch = HybridDetector(stage1=None, verifier=None)  # only _blend is used
        for w in blend_weights:
            scratch.blend_weight = w
            rescored = []
            for r in records:
                p_vlm = _vlm_probability(r)
                if p_vlm is None:
                    rescored.append((r["label"], r["p1"]))
                    continue
                v = VlmVerdict(
                    is_pothole=r["is_pothole"], confidence=r["confidence"],
                    severity=None, rationale="",
                )
                rescored.append((r["label"], scratch._blend(r["p1"], v)))
            curve = _curve(rescored)
            if not curve:
                print(f"  {w:>5.2f} {'-':>11} {'-':>9} {'-':>7}")
                continue
            best_f1, best_thr = 0.0, 0.0
            for tp, (thr, fp) in curve.items():
                prec, rec = tp / (tp + fp), tp / positives
                f1 = 2 * prec * rec / (prec + rec) if prec + rec else 0.0
                if f1 > best_f1:
                    best_f1, best_thr = f1, thr
            print(f"  {w:>5.2f} {max(curve):>11} {best_f1:>9.3f} {best_thr:>7.2f}")


# ── wiring ─────────────────────────────────────────────────────────────────────

def _build_hybrid(args) -> HybridDetector:
    """A hybrid with the gray zone opened to [0,1] so EVERY frame reaches the VLM.

    settings is mutated rather than read from .env so the CLI flags are the single
    source of truth for what ran -- get_verifier() reads settings, and a run that
    silently used a different backend than the flag says would be worse than no
    measurement at all.
    """
    settings.vlm_backend = args.backend
    settings.vlm_model_id = args.vlm_model
    settings.vlm_json_mode = args.json_mode
    if args.vlm_url:
        settings.vlm_http_url = args.vlm_url
    if args.timeout:
        settings.vlm_timeout = args.timeout

    verifier = get_verifier()
    if verifier is None:
        raise SystemExit(f"--backend {args.backend!r} resolves to no verifier.")

    stage1 = OnnxYoloDetector(
        model_path=args.model,
        model_id=Path(args.model).stem,
        conf_threshold=args.conf,
        iou_threshold=args.iou,
        input_size=args.input_size,
        roi_enabled=args.roi,
        roi_top=args.roi_top,
        roi_bottom=args.roi_bottom,
        labels=[c.strip() for c in args.classes.split(",") if c.strip()],
        primary_class_id=args.primary_class,
    )
    return HybridDetector(
        stage1=stage1,
        verifier=verifier,
        low=0.0,
        high=1.0,
        blend_weight=args.blend_weight,
        crop=args.crop,
        crop_margin=args.crop_margin,
        max_calls=10**9,  # the --limit flag is the cost bound, not this
    )


async def main_async(args) -> int:
    cache_path = Path(args.cache) if args.cache else None
    cache: dict = {}
    if cache_path and cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
        print(f"cache: {len(cache)} frame(s) already scored in {cache_path}")

    if args.analyse_only:
        if not cache:
            print("--analyse-only needs a populated --cache.", file=sys.stderr)
            return 2
        _report(list(cache.values()), args.sweep)
        return 0

    pool = await create_pool()
    try:
        rows = [dict(r) for r in await pool.fetch(_LABELLED_SQL)]
    finally:
        await pool.close()

    if args.exclude_ids:
        excluded = {
            line.strip()
            for line in Path(args.exclude_ids).read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        rows = [r for r in rows if r["client_id"] not in excluded]
        print(f"excluded {len(excluded)} training frame(s) from {args.exclude_ids}")

    if args.limit:
        rows = rows[: args.limit]

    todo = [r for r in rows if r["client_id"] not in cache]
    print(f"backend   : {args.backend}  model={args.vlm_model or '(backend default)'}")
    print(f"stage 1   : {args.model}")
    print(f"crop      : {'detections + margin' if args.crop else 'OFF (full frame)'}")
    print(f"frames    : {len(rows)} selected, {len(todo)} need a VLM call "
          f"({len(rows) - len(todo)} cached)\n")
    if not todo:
        print("Nothing to call. Reporting from cache.")
    else:
        hybrid = _build_hybrid(args)
        _score_frames(rows, hybrid, cache, cache_path)

    _report([cache[r["client_id"]] for r in rows if r["client_id"] in cache], args.sweep)
    if cache_path:
        print(f"\ncache written to {cache_path} -- re-run with --analyse-only for free.")
    print("\nNothing was written to the database.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(
        description="Measure a VLM verifier against the human-labelled frames.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument("--model", help="Stage-1 .onnx (required unless --analyse-only)")
    p.add_argument("--backend", default="ollama",
                   help="ollama | openrouter | claude | gemini | local_http (default ollama, "
                        "which is free and keeps imagery on this host)")
    p.add_argument("--vlm-model", default="", help="provider model id; backend default if empty")
    p.add_argument("--vlm-url", default="", help="override the backend's default endpoint")
    p.add_argument("--timeout", type=float, default=0.0, help="per-call timeout override")
    p.add_argument("--json-mode", action=argparse.BooleanOptionalAction, default=True,
                   help="send response_format=json_object (default on)")
    p.add_argument("--limit", type=int, default=25,
                   help="frames to score, 0 = all. Defaults to 25 so a first run is a "
                        "smoke test rather than a surprise bill")
    p.add_argument("--cache", help="JSON file of verdicts; resumes and makes re-analysis free")
    p.add_argument("--analyse-only", action="store_true",
                   help="report from --cache without calling anything (no model needed)")
    p.add_argument("--sweep", type=float, nargs="*",
                   default=[0.0, 0.3, 0.5, 0.7, 0.9, 1.0],
                   help="blend weights to report; recomputed from the cache, so free")
    p.add_argument("--exclude-ids", help="file of client_ids to skip, one per line")
    crop = p.add_mutually_exclusive_group()
    crop.add_argument("--crop", dest="crop", action="store_true",
                      help="crop to the Stage-1 boxes before the VLM call (default)")
    crop.add_argument("--no-crop", dest="crop", action="store_false",
                      help="send the full frame -- the ablation")
    p.set_defaults(crop=True)
    p.add_argument("--crop-margin", type=float, default=0.20)
    p.add_argument("--blend-weight", type=float, default=0.7)
    p.add_argument("--conf", type=float, default=0.05,
                   help="Stage-1 floor. 0.05, not 0.25: this detector's useful range is low")
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

    if not args.analyse_only:
        if not args.model:
            print("--model is required unless --analyse-only.", file=sys.stderr)
            return 2
        if not Path(args.model).exists():
            print(f"Model not found: {args.model}", file=sys.stderr)
            return 2
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
