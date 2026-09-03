"""A per-user, fail-closed quota for metered actions.

WHY NOT `app/middleware/rate_limit.py`. That module is the right shape and the wrong
policy. Three mismatches, each decisive:

  * it is keyed on `device_id`, and the caller here is a staff account;
  * it knows only the resources `events` and `frames`, and silently allows anything
    else (`_LIMITS.get(resource)` returning None means unlimited);
  * **it fails OPEN.** Deliberately, and correctly, for ingestion: dropping a drive
    because the database hiccuped is worse than letting one request through. For an
    outbound paid API call the opposite holds, so this fails CLOSED.

The accounting is lifted from it verbatim, because the reasoning behind that SQL is
worth reusing exactly: one statement, so the increment and the total cannot race, and
the sum split so the data-modifying CTE's own row is not missed. A data-modifying CTE
and the rest of its statement share one snapshot, so a plain `SELECT sum(...)` would
not see the row just written and would undercount by exactly this request.

WHAT USES IT. Nothing yet. `vlm_max_calls_per_run` is a per-INSTANCE counter and an
instance is one job run, so a fresh `get_detector()` per request would reset it to
zero -- N requests would be N uncapped calls. This is the piece that has to exist
before an on-demand VLM endpoint can, and it is deliberately built ahead of the
endpoint rather than bolted on after the first bill.
"""

from __future__ import annotations

import logging

import asyncpg

logger = logging.getLogger(__name__)

_WINDOW = "1 hour"

# One statement. See the module docstring, and rate_limit.py's own comment, for why
# the sum is split rather than written as a single aggregate.
_CONSUME_SQL = f"""
WITH bucket AS (
    INSERT INTO user_quota (user_id, resource, window_start, count)
    VALUES ($1, $2, date_trunc('minute', now()), $3)
    ON CONFLICT (user_id, resource, window_start)
    DO UPDATE SET count = user_quota.count + EXCLUDED.count
    RETURNING count
)
SELECT
    (SELECT count FROM bucket)
    + COALESCE((
        SELECT sum(count) FROM user_quota
        WHERE user_id = $1
          AND resource = $2
          AND window_start >  date_trunc('minute', now()) - interval '{_WINDOW}'
          AND window_start <  date_trunc('minute', now())
      ), 0) AS total
"""

_RELEASE_SQL = """
UPDATE user_quota SET count = greatest(count - $3, 0)
WHERE user_id = $1 AND resource = $2 AND window_start = date_trunc('minute', now())
"""

PRUNE_SQL = f"""
DELETE FROM user_quota
WHERE window_start < date_trunc('minute', now()) - interval '{_WINDOW}'
"""


class QuotaExceededError(Exception):
    """The caller has used its allowance for this window."""

    def __init__(self, resource: str, used: int, limit: int) -> None:
        super().__init__(f"{resource} quota exhausted: {used} of {limit} in the last hour")
        self.resource = resource
        self.used = used
        self.limit = limit


async def consume(
    pool: asyncpg.Pool,
    user_id: str,
    resource: str,
    limit: int,
    count: int = 1,
) -> int:
    """Claim `count` units. Returns the running total, or raises.

    Raises `QuotaExceededError` when the claim would exceed `limit`, and **also raises it
    when the accounting itself fails** — that is the fail-closed half, and it is the
    whole reason this module exists separately from the ingestion limiter. A database
    error here means the quota is unknown, and an unknown quota on a paid call has to
    be treated as an exhausted one.

    The claim is made BEFORE the work, so a crash mid-call leaves the unit spent. That
    is the safe direction for something metered: `release` exists for the case where
    the caller knows the work never started.
    """
    if limit <= 0:
        raise QuotaExceededError(resource, 0, limit)
    try:
        async with pool.acquire() as conn:
            total = await conn.fetchval(_CONSUME_SQL, user_id, resource, count)
    except Exception:
        # Deliberately broad, and deliberately not swallowed. rate_limit.py catches
        # the same class and returns "allowed"; here the opposite is correct.
        logger.exception("Quota accounting failed for %s/%s; refusing.", user_id, resource)
        raise QuotaExceededError(resource, limit, limit) from None

    used = int(total or 0)
    if used > limit:
        # Give back what this call claimed, so a refused request does not burn
        # allowance it never used. Best-effort: the refusal stands either way.
        await release(pool, user_id, resource, count)
        raise QuotaExceededError(resource, used, limit)
    return used


async def release(pool: asyncpg.Pool, user_id: str, resource: str, count: int = 1) -> None:
    """Hand back units the caller claimed but did not use. Never raises.

    Only correct when the work provably did not happen. `greatest(count - $3, 0)`
    because a concurrent prune could have removed the bucket, and a negative count
    would corrupt every later total in the window.
    """
    try:
        async with pool.acquire() as conn:
            await conn.execute(_RELEASE_SQL, user_id, resource, count)
    except Exception:
        logger.warning("Could not release %d %s unit(s) for %s", count, resource, user_id)
