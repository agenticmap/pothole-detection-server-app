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

3. Authorization is enforced here, not only in the route's role dependency.
   Until migrations/009 added asset_cluster.org_id, any staff member of any org
   could repair any city's clusters. The check has to happen after the row is
   locked -- reading the owner, deciding, and then writing in separate
   transactions would let a concurrent re-assignment slip between them -- so it
   sits inside the same transaction as the FOR UPDATE rather than in the route.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from uuid import uuid4

import asyncpg

logger = logging.getLogger(__name__)

# FOR UPDATE serialises two operators clicking at once, and holds the row across
# the authorization decision below. Split from the write deliberately: the owner
# has to be read under the same lock that the UPDATE runs in.
_LOCK_SQL = """
SELECT cluster_id, repaired_at, org_id
FROM asset_cluster
WHERE cluster_id = $1
FOR UPDATE
"""

# $1=cluster_id $2=repaired $3=repair_id $4=note $5=user_id $6=org_id
_REPAIR_SQL = """
WITH upd AS (
    UPDATE asset_cluster c
       SET repaired_at = CASE WHEN $2::boolean THEN now() ELSE NULL END,
           updated_at  = now()
     WHERE c.cluster_id = $1
       -- Only when the state actually changes: currently-open (repaired_at IS
       -- NULL) must differ from the requested state. Redundant with the caller's
       -- check now that the row is locked, but kept so the invariant survives
       -- any future caller that forgets it.
       AND (c.repaired_at IS NULL) = $2::boolean
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
    (SELECT count(*) FROM upd)::int AS changed,
    (SELECT repaired_at FROM upd)   AS new_repaired_at
"""

# Repairing an unowned (org_id IS NULL) cluster takes an admin. Everything else
# requires the caller's org to match the cluster's owner.
_ADMIN_ROLE = "admin"


@dataclass(frozen=True)
class RepairOutcome:
    """What actually happened, so the route can pick a status code."""

    found: bool
    changed: bool
    repaired_at: datetime | None
    repair_id: str | None
    authorized: bool = True


async def set_repair_state(
    pool: asyncpg.Pool,
    *,
    cluster_id: str,
    repaired: bool,
    note: str | None,
    user_id: str,
    org_id: str | None,
    role: str,
) -> RepairOutcome:
    """Mark a cluster repaired or reopen it. Returns what changed.

    ``found=False``      → no such cluster (404).
    ``authorized=False`` → the caller's org does not own it (403).
    ``changed=False``    → already in the requested state; nothing written, no audit row.

    Ownership rules (migrations/009):
      * cluster.org_id matches the caller's org → allowed at the route's role floor.
      * cluster.org_id IS NULL (unowned backlog) → 'admin' only.
      * otherwise → refused, even for an admin of another org.
    """
    repair_id = f"rpr_{uuid4().hex}"

    async with pool.acquire() as conn:
        # One transaction: the row stays locked from the ownership read through
        # the write, so the owner cannot change underneath the decision.
        async with conn.transaction():
            target = await conn.fetchrow(_LOCK_SQL, cluster_id)
            if target is None:
                return RepairOutcome(
                    found=False, changed=False, repaired_at=None, repair_id=None
                )

            owner = target["org_id"]
            if owner is None:
                permitted = role == _ADMIN_ROLE
            else:
                permitted = owner == org_id

            if not permitted:
                logger.warning(
                    "Refused cross-org repair: cluster=%s owner=%s caller_org=%s "
                    "user=%s role=%s",
                    cluster_id, owner, org_id, user_id, role,
                )
                return RepairOutcome(
                    found=True,
                    changed=False,
                    repaired_at=target["repaired_at"],
                    repair_id=None,
                    authorized=False,
                )

            row = await conn.fetchrow(
                _REPAIR_SQL, cluster_id, repaired, repair_id, note, user_id, org_id
            )

    changed = bool(row["changed"])
    # On a no-op the UPDATE returned nothing, so the current value is the one the
    # row already had.
    repaired_at = row["new_repaired_at"] if changed else target["repaired_at"]

    if changed:
        logger.info(
            "Cluster repair state changed: cluster=%s repaired=%s user=%s org=%s",
            cluster_id, repaired, user_id, org_id,
        )
    return RepairOutcome(
        found=True,
        changed=changed,
        repaired_at=repaired_at,
        repair_id=repair_id if changed else None,
    )
