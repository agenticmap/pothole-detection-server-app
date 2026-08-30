"""Backfill server-side detection over frames already in the database.

Phase 2.7. Runs the real detection worker (app/detection/service.py) with a
detector built from these CLI flags rather than from DETECTION_* env vars, so a
backfill can never quietly use a different model than the command says.

    # See what would happen, write nothing.
    python scripts/backfill_detection.py --model models/pothole.onnx --dry-run

    # Score every undetected frame, then let fusion re-score the pairs.
    python scripts/backfill_detection.py --model models/pothole.onnx

    # Re-score frames a previous run already did (e.g. a better model).
    python scripts/backfill_detection.py --model models/v2.onnx --redo

Why a script rather than just DETECTION_ENABLED=true:

- **It is a one-off bulk job, not a steady-state trickle.** The scheduler ticks
  every 2 minutes for whatever has arrived since; pushing 2900 frames through it
  means ~25 minutes of the API worker also doing CPU inference.
- **The model needs proving before it writes 2900 rows.** Run
  scripts/detect_eval.py first — it writes nothing.
- **Fusion needs a nudge afterwards** (see --reset-fusion below), which the
  scheduler will never do on its own.

The advisory lock (0x504F56) is shared with the scheduled job, so this is safe to
run against a live server: one of the two will wait rather than double-score.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Run directly (`python scripts/backfill_detection.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Windows consoles default to cp1252, and several messages below contain an arrow
# or an em dash. Without this the script raises UnicodeEncodeError when it prints
# them -- which on this path means *after* every frame has already been scored.
# Harmless on POSIX, where stdout is already UTF-8.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


from app.config import settings  # noqa: E402
from app.database import create_pool, run_migrations  # noqa: E402
from app.detection.onnx_v1 import OnnxYoloDetector  # noqa: E402
from app.detection.service import run_detection_job  # noqa: E402

_COUNTS_SQL = """
SELECT
    count(*)                                                    AS frames,
    count(*) FILTER (WHERE detected_at IS NOT NULL)             AS detected,
    count(*) FILTER (WHERE server_probability IS NOT NULL)      AS scored,
    count(*) FILTER (WHERE detected_at IS NOT NULL
                       AND server_probability IS NULL)          AS failed
FROM asset_frame
"""

# The comparison the phase exists to produce: does the server model disagree with
# the phone, and in which direction?
_COMPARE_SQL = """
SELECT
    round(avg(device_probability)::numeric, 4)                          AS device_mean,
    round(avg(server_probability)::numeric, 4)                          AS server_mean,
    count(*) FILTER (WHERE server_probability > device_probability)      AS server_higher,
    count(*) FILTER (WHERE server_probability < device_probability)      AS server_lower,
    count(*) FILTER (WHERE server_probability >= $1)                     AS server_over_thresh,
    count(*) FILTER (WHERE device_probability >= $1)                     AS device_over_thresh,
    count(*) FILTER (WHERE server_probability BETWEEN $2 AND $3)         AS gray_zone
FROM asset_frame
WHERE server_probability IS NOT NULL AND device_probability IS NOT NULL
"""

# fusion selects on processed_at IS NULL and upserts on (event, frame), so clearing
# the flag re-scores existing pairs in place instead of creating duplicates.
_RESET_FUSION_SQL = """
UPDATE asset_frame SET processed_at = NULL
WHERE detected_at IS NOT NULL AND server_probability IS NOT NULL
"""

_CLEAR_DETECTED_SQL = """
UPDATE asset_frame
SET detected_at = NULL, server_probability = NULL, server_model_id = NULL,
    server_detections = NULL
"""


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    p.add_argument("--model", required=True, help="path to the .onnx export")
    p.add_argument("--model-id", default=settings.detection_model_id,
                   help="written to asset_frame.server_model_id")
    p.add_argument("--limit", type=int, default=0, help="stop after N frames (0 = all)")
    p.add_argument("--chunk", type=int, default=100, help="frames per worker call")
    p.add_argument("--dry-run", action="store_true", help="score nothing, just report the queue")
    p.add_argument("--redo", action="store_true",
                   help="clear detected_at/server_* on EVERY frame first, then re-score")
    p.add_argument("--conf", type=float, default=settings.detection_conf_threshold)
    p.add_argument("--iou", type=float, default=settings.detection_iou_threshold)
    p.add_argument("--input-size", type=int, default=settings.detection_input_size)

    roi = p.add_mutually_exclusive_group()
    roi.add_argument("--roi", dest="roi", action="store_true")
    roi.add_argument("--no-roi", dest="roi", action="store_false")
    p.set_defaults(roi=settings.detection_roi_enabled)
    p.add_argument("--roi-top", type=float, default=settings.detection_roi_top)
    p.add_argument("--roi-bottom", type=float, default=settings.detection_roi_bottom)
    p.add_argument("--classes", default="pothole",
                   help="comma-separated class names; position is the class_id. Must match "
                        "the model's data.yaml, or boxes are mislabelled and the frame "
                        "probability can come from the wrong class")
    p.add_argument("--primary-class", type=int, default=0,
                   help="index of the class that sets the frame probability (default 0)")

    p.add_argument("--reset-fusion", dest="reset_fusion", action="store_true",
                   help="clear processed_at so fusion re-scores pairs (default)")
    p.add_argument("--no-reset-fusion", dest="reset_fusion", action="store_false")
    p.set_defaults(reset_fusion=True)
    return p.parse_args()


async def _report(conn) -> None:
    row = await conn.fetchrow(_COUNTS_SQL)
    print(f"  frames {row['frames']}   detected {row['detected']}   "
          f"scored {row['scored']}   failed {row['failed']}")


async def main() -> int:
    args = _parse_args()
    if not Path(args.model).exists():
        print(f"Model not found: {args.model}", file=sys.stderr)
        return 2

    detector = OnnxYoloDetector(
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

    db = settings.database_url.rsplit("/", 1)[-1]
    roi = f"on ({args.roi_top}-{args.roi_bottom})" if args.roi else "off"
    print(f"database={db}  model={args.model}  model_id={args.model_id}")
    print(f"conf={args.conf}  iou={args.iou}  roi={roi}")

    pool = await create_pool()
    try:
        await run_migrations(pool)
        async with pool.acquire() as conn:
            print("\nBefore:")
            await _report(conn)

            if args.redo:
                if args.dry_run:
                    print("\n--redo with --dry-run: would clear detected_at on every frame.")
                else:
                    result = await conn.execute(_CLEAR_DETECTED_SQL)
                    print(f"\n--redo: cleared previous detection results ({result}).")

            pending = await conn.fetchval(
                "SELECT count(*) FROM asset_frame WHERE detected_at IS NULL"
            )
        print(f"\n{pending} frames to score.")
        if args.dry_run:
            print("--dry-run: stopping before inference.")
            return 0
        if not pending:
            return 0

        # run_detection_job takes its batch size from settings; a smaller chunk keeps
        # progress visible and bounds how long the advisory lock is held at a stretch.
        settings.detection_batch_size = args.chunk

        started = time.monotonic()
        total = 0
        while True:
            n = await run_detection_job(pool, detector=detector)
            if n == 0:
                break
            total += n
            rate = total / max(time.monotonic() - started, 1e-6)
            print(f"  {total}/{pending}  ({rate:.1f} frames/s)")
            if args.limit and total >= args.limit:
                print(f"  stopping at --limit {args.limit}")
                break

        elapsed = time.monotonic() - started
        print(f"\nScored {total} frames in {elapsed:.0f}s ({total / max(elapsed, 1e-6):.1f}/s)")

        async with pool.acquire() as conn:
            print("\nAfter:")
            await _report(conn)

            cmp_row = await conn.fetchrow(
                _COMPARE_SQL, args.conf, settings.vlm_verify_low, settings.vlm_verify_high
            )
            if cmp_row and cmp_row["server_mean"] is not None:
                print("\nServer vs device, over frames both scored:")
                print(f"  mean probability     device {cmp_row['device_mean']}   "
                      f"server {cmp_row['server_mean']}")
                print(f"  server scored higher {cmp_row['server_higher']}, "
                      f"lower {cmp_row['server_lower']}")
                print(f"  at or above conf {args.conf}: "
                      f"device {cmp_row['device_over_thresh']}, "
                      f"server {cmp_row['server_over_thresh']}")
                print(f"  in the VLM gray zone "
                      f"[{settings.vlm_verify_low}, {settings.vlm_verify_high}]: "
                      f"{cmp_row['gray_zone']}")
            disagreements = await conn.fetchval("SELECT count(*) FROM model_disagreement")
            print(f"  model_disagreement rows: {disagreements} "
                  f"(threshold {settings.detection_disagreement_threshold})")

            if args.reset_fusion:
                # Pairs computed before detection kept a fused_confidence derived from
                # device_probability. Clearing processed_at makes the next fusion tick
                # recompute them through COALESCE(server_probability, device_probability).
                result = await conn.execute(_RESET_FUSION_SQL)
                print(f"\nCleared processed_at so fusion re-scores existing pairs ({result}).")
                print("  The next fusion tick (or a manual run_fusion_job) will upsert them")
                print("  in place — fusion_pair's PK is (event, frame), so no duplicates.")
            else:
                print("\n--no-reset-fusion: existing fusion_pair rows keep their "
                      "device-probability scores.")
    finally:
        await pool.close()

    print("\nNext: scripts/detect_eval.py --labels for precision/recall against ground truth.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
