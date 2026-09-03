"""Which frames a labelling pass puts in front of a human, and in what order.

Extracted from `scripts/label_frames.py` so the CLI and the operator console's review
surface cannot drift into two different answers. The rules here are not ergonomics --
each one is a countermeasure against a *measured* failure:

  - `wants_frame`'s box-mode rule (never queue an unjudged frame) exists because
    drawing a box before deciding what a frame IS anchors the verdict to the box.
  - `is_done` keys on `boxed_at` rather than "does the frame have boxes", because a
    frame reviewed and found genuinely clean has zero boxes and IS finished, while a
    frame nobody opened also has zero boxes and is NOT. Collapsing those two is the
    mistake Phase 2.7b exists to prevent -- the exporter may only ship the first kind
    as a YOLO background image.
  - `rank_by_score` orders and never filters: the detector's binding constraint is a
    shortage of in-domain POSITIVES, and even the 0.00-0.05 band held 19 of the 65
    known potholes, so any low-score cutoff discards the data the model most needs.

Everything here is pure: it takes mappings (asyncpg `Record` and plain `dict` both
work) and returns lists. No database, no printing, no `sys`. Callers own their own
reporting -- which is why `rank_by_score` reports nothing and the CLI wraps it.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from typing import Any

# Local hours (UTC-4, Toronto) counted as daylight. Night frames are a different
# detection problem -- rain on glass, headlight glare -- and must be measured apart.
DAY_HOURS = range(6, 20)
LOCAL_OFFSET_HOURS = 4

Row = Mapping[str, Any]


def is_done(row: Row, *, box: bool) -> bool:
    """Has this frame had the thing the current mode produces?"""
    return row["boxed_at"] is not None if box else row["label"] is not None


def wants_frame(row: Row, *, box: bool, review: bool) -> bool:
    """Does this row belong in the queue?

    `review` is a check-my-work mode, not an "include everything" switch: it queues
    ONLY finished frames. Mixing finished and outstanding work in one queue means
    paging past completed frames to reach the next real one, which is the whole
    complaint this function exists to answer. The two jobs stay separate.

    box + review     -> only frames already signed off
    box              -> only judged frames NOT yet signed off  (the pass itself)
    verdict + review -> only frames already labelled
    verdict          -> only frames not yet labelled
    """
    # Box mode never queues an unjudged frame in either direction: drawing a box
    # before deciding what the frame IS is exactly the anchoring this tool avoids.
    if box and row["label"] is None:
        return False
    return is_done(row, box=box) if review else not is_done(row, box=box)


def stratum(row: Row) -> tuple[int, str]:
    """The (device-probability decile, day/night) bucket a frame belongs to."""
    p = row["device_probability"]
    decile = 0 if p is None else min(9, int(p * 10))
    hour = (row["ts_utc"].hour - LOCAL_OFFSET_HOURS) % 24
    return decile, "day" if hour in DAY_HOURS else "night"


def stratified(rows: Sequence[Row], count: int) -> list:
    """Round-robin across (decile, day/night) buckets until `count` is reached."""
    buckets: dict[tuple[int, str], list] = defaultdict(list)
    for r in rows:
        buckets[stratum(r)].append(r)

    picked, keys = [], sorted(buckets)
    i = 0
    while len(picked) < count and any(buckets[k] for k in keys):
        k = keys[i % len(keys)]
        if buckets[k]:
            picked.append(buckets[k].pop(0))
        i += 1
    return picked


def rank_by_score(rows: Sequence[Row], count: int) -> list:
    """Highest `server_probability` first; unscored frames excluded, not ranked last.

    A NULL score means the backfill has not seen the frame. Sorting it as if it were
    0.0 would bury real frames under unscored ones, so those rows are dropped from a
    score-ordered pass entirely -- the caller is expected to say so (the CLI prints a
    warning; the API reports the count in its response). This function itself stays
    silent so it can be used from a request handler.

    Ranking, never filtering: measured over 340 labelled frames the best band is a
    1.96x lift over base rate, which is useful for ordering a queue and useless as a
    decision rule. There is deliberately no auto-accept or auto-reject here.
    """
    scored = [r for r in rows if r["server_probability"] is not None]
    scored.sort(key=lambda r: r["server_probability"], reverse=True)
    return scored[:count]


def count_scored(rows: Sequence[Row]) -> int:
    """How many rows carry a `server_probability` -- what a caller needs to report
    "N frame(s) are unscored and excluded" without re-deriving `rank_by_score`'s filter."""
    return sum(1 for r in rows if r["server_probability"] is not None)
