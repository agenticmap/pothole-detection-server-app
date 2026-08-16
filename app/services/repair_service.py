"""Write path for cluster repair state (Phase 2.5).

The only place in app/ that mutates asset_cluster outside the clustering job.
Kept in its own module so that stays auditable.

Two things here are load-bearing and easy to get wrong:

1. "Idempotent" must mean *no-op*, not *re-stamp*. _MEMBERS_CTE in the clustering
   job excludes observations older than a nearby repaired_at, so re-stamping
   repaired_at = now() on a second `repaired: true` would move that exclusion
   window forward and retroactively swallow every observation recorded between
   the two calls — which is exactly the "the defect came back" evidence the
   system exists to capture. A double-click would erase it. The UPDATE therefore
   fires only when the state actually changes, and only then is an audit row
   written.

2. updated_at must be bumped. 007_tiles.sql added idx_asset_cluster_updated_at
   for change polling and cluster_query_service._FILTER selects on
   `updated_at > $since`; without the bump an incremental client never learns
   about the repair.

   (Related pre-existing gap, not fixed here: because that filter also has
   `repaired_at IS NULL`, an incremental client sees a repaired cluster simply
   vanish rather than receiving a tombstone.)
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import asyncpg

logger = logging.getLogger(__name__)

# FOR UPDATE serialises two operators clicking at once. The whole thing is one
# statement, so it runs in asyncpg's implicit transaction.
# $1=cluster_id $2=repaired $3=repair_id $4=note $5=user_id $6=org_id
_REPAIR_SQL = """
WITH target AS (
    SELECT cluster_id, repaired_at
    FROM asset_cluster
    WHERE cluster_id = $1
    FOR UPDATE
), upd AS (
    UPDATE asset_cluster c
       SET repaired_at = CASE WHEN $2::boolean THEN now() ELSE NULL END,
           updated_at  = now()
      FROM target t
     WHERE c.cluster_id = t.cluster_id
       -- Only when the state actually changes: currently-open (repaired_at IS
       -- NULL) must differ from the requested state.
       AND (t.repaired_at IS NULL) = $2::boolean
    RETURNING c.cluster_id, c.repaired_at
), logged AS (
    INSERT INTO repair_log (
        repair_id, cluster_id, action, note, user_id, org_id, repaired_at
    )
    SELECT $3, u.cluster_id,
           CASE WHEN $2::boolean THEN 'repaired' ELSE 'unrepaired' END,
           $4, $5, $6, u.repaired_at
    FROM upd u
    RETURNING repair_id
)
SELECT
    (SELECT count(*) FROM target)::int AS found,
    (SELECT count(*) FROM upd)::int    AS changed,
    (SELECT repaired_at FROM target)   AS previous_repaired_at,
    (SELECT repaired_at FROM upd)      AS new_repaired_at
"""


@dataclass(frozen=True)
class RepairOutcome:
    """What actually happened, so the route can pick a status code."""

    found: bool
    changed: bool
    repaired_at: datetime | None
    repair_id: str | None


async def set_repair_state(
    pool: asyncpg.Pool,
    *,
    cluster_id: str,
    repaired: bool,
    note: str | None,
    user_id: str,
    org_id: str | None,
) -> RepairOutcome:
    """Mark a cluster repaired or reopen it. Returns what changed.

    ``found=False``  → no such cluster (404).
    ``changed=False`` → already in the requested state; nothing written, no audit row.
    """
    repair_id = f"rpr_{uuid4().hex}"

    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            _REPAIR_SQL, cluster_id, repaired, repair_id, note, user_id, org_id
        )

    found = bool(row["found"])
    changed = bool(row["changed"])
    # On a no-op the UPDATE returned nothing, so the current value is the one the
    # row already had.
    repaired_at = row["new_repaired_at"] if changed else row["previous_repaired_at"]

    if changed:
        logger.info(
            "Cluster repair state changed: cluster=%s repaired=%s user=%s org=%s",
            cluster_id, repaired, user_id, org_id,
        )
    return RepairOutcome(
        found=found,
        changed=changed,
        repaired_at=repaired_at,
        repair_id=repair_id if changed else None,
    )
