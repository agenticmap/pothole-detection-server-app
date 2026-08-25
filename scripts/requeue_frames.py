"""Re-fuse stored frames — clear `processed_at` and drain the fusion queue.

Phase 2.2d. This is the activation path: the pairing search only re-decides a
frame's partner when that frame goes back through `run_fusion_job`, and
`_SELECT_FRAME_BATCH_SQL` selects on `processed_at IS NULL`.

Use this after changing any FUSION_* pairing knob (the lead band, the windows, the
cost weights, or FUSION_PAIRING_COST_ENABLED). Without it the config change applies
only to frames that arrive from now on, and the table stays a mixture of two
rankings with nothing marking which row came from which.

`scripts/backfill_detection.py` also clears `processed_at`, but it requires
`--model` because its job is to score frames first. This script scores nothing.

WHAT IT WRITES: `asset_frame.processed_at`, and `fusion_pair` via the job. Both are
derived — `fusion_pair` is rebuilt entirely from `asset_frame` + `asset_observation`,
and neither of those is touched. Nothing here is destructive to collected data.

Usage (from the repo root):

    python scripts/requeue_frames.py --dry-run     # what would be requeued
    python scripts/requeue_frames.py               # requeue everything and drain
    python scripts/requeue_frames.py --device abc  # one device only
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Run directly (`python scripts/requeue_frames.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402
from app.fusion.service import run_fusion_job  # noqa: E402

_COUNT_SQL = """
SELECT count(*) FROM asset_frame
WHERE ($1::text IS NULL OR device_id = $1)
"""

_PENDING_SQL = """
SELECT count(*) FROM asset_frame
WHERE processed_at IS NULL AND ($1::text IS NULL OR device_id = $1)
"""

_REQUEUE_SQL = """
UPDATE asset_frame SET processed_at = NULL
WHERE processed_at IS NOT NULL AND ($1::text IS NULL OR device_id = $1)
"""

_BEFORE_AFTER_SQL = """
SELECT
    count(*)                                        AS pairs,
    count(DISTINCT event_client_id)                 AS observations,
    count(*) FILTER (WHERE is_primary)              AS primaries,
    count(*) FILTER (WHERE match_cost IS NOT NULL)  AS costed,
    avg(delta_m)                                    AS mean_delta_m,
    avg(delta_ms)                                   AS mean_delta_ms,
    count(*) FILTER (WHERE delta_ms > 0)            AS forward_picks
FROM fusion_pair
"""


def _report(label: str, row) -> None:
    print(f"  {label:<8} pairs={row['pairs']:<6} observations={row['observations']:<5} "
          f"primaries={row['primaries']:<5} costed={row['costed']}")
    if row["pairs"]:
        print(f"           mean delta_m={row['mean_delta_m']:.2f} m  "
              f"mean delta_ms={row['mean_delta_ms']:.1f}  "
              f"forward picks={row['forward_picks']}")


async def main_async(args) -> int:
    pool = await create_pool()
    try:
        total = await pool.fetchval(_COUNT_SQL, args.device)
        if not total:
            scope = f" for device {args.device}" if args.device else ""
            print(f"No frames{scope}. Nothing to do.")
            return 0

        before = await pool.fetchrow(_BEFORE_AFTER_SQL)
        print(f"{total} frame(s) in scope"
              + (f" (device {args.device})" if args.device else " (all devices)"))
        print(f"Pairing: cost ranking "
              f"{'ON' if settings.fusion_pairing_cost_enabled else 'OFF'}, "
              f"lead band {settings.fusion_lead_near_m:g}-{settings.fusion_lead_far_m:g} m, "
              f"window {settings.fusion_window_m:g} m / "
              f"<= {settings.fusion_window_ms_max} ms")
        print()
        _report("before", before)

        if args.dry_run:
            print()
            print("--dry-run: nothing written. Re-run without it to requeue and drain.")
            return 0

        requeued = await pool.execute(_REQUEUE_SQL, args.device)
        print()
        print(f"Requeued ({requeued}). Draining in batches of "
              f"{settings.fusion_batch_size}...")

        started = time.monotonic()
        for tick in range(1, args.max_ticks + 1):
            pending = await pool.fetchval(_PENDING_SQL, args.device)
            if not pending:
                break
            await run_fusion_job(pool)
            after_pending = await pool.fetchval(_PENDING_SQL, args.device)
            print(f"  tick {tick}: {pending} -> {after_pending} pending")
            if after_pending == pending:
                # Expected when frames are younger than fusion_retry_grace_minutes:
                # an unpaired frame is deliberately held back for its event to
                # arrive. Stop rather than spin.
                print(f"  no progress -- {after_pending} frame(s) are unpaired and "
                      f"still inside the {settings.fusion_retry_grace_minutes}-minute "
                      "retry grace. They will drain on a later tick.")
                break
        else:
            print(f"  hit --max-ticks ({args.max_ticks}); run again to continue.")

        after = await pool.fetchrow(_BEFORE_AFTER_SQL)
        print()
        _report("before", before)
        _report("after", after)
        print()
        print(f"Done in {time.monotonic() - started:.1f}s. "
              "Run the clustering job (or wait for its tick) to see the effect "
              "on asset_cluster.")
        return 0
    finally:
        await pool.close()


def main() -> int:
    p = argparse.ArgumentParser(
        description="Clear processed_at and re-run fusion over stored frames.",
    )
    p.add_argument("--dry-run", action="store_true",
                   help="report what would be requeued, write nothing")
    p.add_argument("--device", default=None,
                   help="limit to one device_id (default: all)")
    p.add_argument("--max-ticks", type=int, default=200,
                   help="safety bound on drain iterations (default 200)")
    return asyncio.run(main_async(p.parse_args()))


if __name__ == "__main__":
    sys.exit(main())
