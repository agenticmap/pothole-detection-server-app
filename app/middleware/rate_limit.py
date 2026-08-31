"""Per-device sliding-window rate limiter, backed by `device_rate_limit`.

Counts live in Postgres, not in process memory, because the Dockerfile runs
`uvicorn --workers 2`. The previous implementation kept module-level dicts, so
each worker enforced its own private ceiling: the effective limit was **doubled**
and applied inconsistently depending on which worker a request happened to land
on. The dict also never evicted a `device_id`, so it grew for the life of the
process.

`migrations/001_initial_schema.sql` has carried the `device_rate_limit` table for
exactly this since the beginning; nothing had ever used it.

Limits (`app/config.py`): `RATE_LIMIT_EVENTS_PER_HOUR` and
`RATE_LIMIT_FRAMES_PER_HOUR`, both **5000/hour**. Sized for a real collection
drive, not a demo — a drive's buffered data drains in one burst when the phone
rejoins Wi-Fi, and the original 100/hour 429'd mid-drain and made the client
retry the same rows forever.

## Failure policy: fail OPEN, and say so

If the quota query errors the request is allowed, with an ERROR logged. A device
that cannot upload loses collected drive data permanently; a device that briefly
overshoots its quota costs a few rows of disk. The asymmetry is not close, so
this deliberately does not fail closed.
"""

from __future__ import annotations

import logging

import asyncpg
from fastapi import HTTPException

from app.config import settings

logger = logging.getLogger(__name__)

_WINDOW = "1 hour"

# One statement, because the increment and the total must not race each other.
#
# The sum is split deliberately. A data-modifying CTE and the rest of the same
# statement share one snapshot, so a plain `SELECT sum(...)` here would NOT see
# the row the CTE just wrote and would undercount by exactly this request. So the
# current bucket's post-increment value comes back via RETURNING, and only
# STRICTLY OLDER buckets are summed from the table.
_CONSUME_SQL = f"""
WITH bucket AS (
    INSERT INTO device_rate_limit (device_id, resource, window_start, request_count)
    VALUES ($1, $2, date_trunc('minute', now()), $3)
    ON CONFLICT (device_id, resource, window_start)
    DO UPDATE SET request_count = device_rate_limit.request_count + EXCLUDED.request_count
    RETURNING request_count
)
SELECT
    (SELECT request_count FROM bucket)
    + COALESCE((
        SELECT sum(request_count) FROM device_rate_limit
        WHERE device_id = $1
          AND resource = $2
          AND window_start >  date_trunc('minute', now()) - interval '{_WINDOW}'
          AND window_start <  date_trunc('minute', now())
      ), 0) AS total
"""

# Buckets older than the window can never contribute again. Pruned by the
# retention job rather than on the request path: a DELETE per upload would put a
# write amplification on ingestion for no benefit.
PRUNE_SQL = f"""
DELETE FROM device_rate_limit
WHERE window_start < date_trunc('minute', now()) - interval '{_WINDOW}'
"""

_LIMITS = {
    "events": lambda: settings.rate_limit_events_per_hour,
    "frames": lambda: settings.rate_limit_frames_per_hour,
}


async def check_rate_limit(
    pool: asyncpg.Pool, device_id: str, resource: str, count: int = 1
) -> None:
    """Consume `count` from this device's hourly allowance for `resource`.

    Args:
        pool: the shared asyncpg pool.
        device_id: the X-Device-Id header value.
        resource: "events" or "frames". Anything else is not limited.
        count: items in this request (the batch size, for events).

    Raises:
        HTTPException: 429 once the device is over its hourly limit.
    """
    limit_for = _LIMITS.get(resource)
    if limit_for is None:
        return
    limit = limit_for()

    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(_CONSUME_SQL, device_id, resource, count)
    except (asyncpg.PostgresError, OSError) as exc:
        # Fail open. See the module docstring: losing a drive is unrecoverable,
        # overshooting a quota is not.
        logger.error(
            "Rate-limit accounting failed for device=%s resource=%s; ALLOWING the "
            "request unmetered. %s",
            device_id, resource, exc,
        )
        return

    if total is not None and total > limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "device_id": device_id,
                "resource": resource,
                "limit": limit,
                "window": _WINDOW,
                "current": int(total),
            },
        )


async def reset_rate_limits(pool: asyncpg.Pool) -> None:
    """Clear all counters. Tests only — this truncates the table."""
    async with pool.acquire() as conn:
        await conn.execute("TRUNCATE device_rate_limit")
