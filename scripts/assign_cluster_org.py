"""Assign an owner to unowned clusters (the migrations/009 backlog).

`asset_cluster.org_id` was added by migration 009 and deliberately left NULL on
every row: there is no municipal boundary table to assign by geography and no
device->org mapping, so the server cannot infer an owner. NULL means "unowned",
which `repair_service` permits only an `admin` to repair.

The consequence was that a `staff` operator could not mark ANY real detection
repaired -- the clustering job produced every cluster in the database and stamped
none of them, so 100% of them were admin-only. Setting CLUSTER_OWNER_ORG_ID makes
the job stamp clusters it creates from now on; this script is for the ones already
there.

A script rather than a migration on purpose: a migration cannot know which org owns
the backlog, and a migration that guessed would assert ownership that is not real.

Usage:
    python scripts/assign_cluster_org.py --org org_cambridge            # dry run
    python scripts/assign_cluster_org.py --org org_cambridge --apply

Only NULL rows are touched. A cluster that already has an owner is never
reassigned, so this cannot move one city's backlog to another.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

# Run directly (`python scripts/assign_cluster_org.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.database import create_pool  # noqa: E402


async def assign(*, org_id: str, apply: bool, asset_type: str | None) -> int:
    pool = await create_pool()
    try:
        async with pool.acquire() as conn:
            if await conn.fetchval("SELECT 1 FROM org WHERE org_id = $1", org_id) is None:
                # The FK would refuse the UPDATE anyway; say why rather than
                # surfacing a constraint violation.
                raise SystemExit(
                    f"No such org: {org_id!r}. Create it first "
                    f"(scripts/create_staff.py --org {org_id} ...)."
                )

            # Two statements, two placeholder numberings: the UPDATE spends $1 on the
            # new owner and the count does not, so the asset_type filter cannot share
            # a single predicate string.
            count_filter = "org_id IS NULL" + (
                " AND asset_type = $1" if asset_type is not None else ""
            )
            update_filter = "org_id IS NULL" + (
                " AND asset_type = $2" if asset_type is not None else ""
            )
            count_args = [asset_type] if asset_type is not None else []
            update_args: list[object] = [org_id, *count_args]

            unowned = await conn.fetchval(
                f"SELECT count(*) FROM asset_cluster WHERE {count_filter}", *count_args
            )
            owned_elsewhere = await conn.fetchval(
                "SELECT count(*) FROM asset_cluster WHERE org_id IS NOT NULL AND org_id <> $1",
                org_id,
            )

            print(f"unowned clusters matching     : {unowned}")
            print(f"already owned by another org  : {owned_elsewhere} (never touched)")

            if not apply:
                print("\nDry run. Re-run with --apply to write.")
                return 0

            updated = await conn.execute(
                f"UPDATE asset_cluster SET org_id = $1, updated_at = now() "
                f"WHERE {update_filter}",
                *update_args,
            )
            # asyncpg returns the command tag, e.g. "UPDATE 204".
            count = int(updated.rsplit(" ", 1)[-1])
            print(f"\nassigned {count} cluster(s) to {org_id}")
            return count
    finally:
        await pool.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", required=True, help="org_id to assign ownership to")
    parser.add_argument(
        "--asset-type",
        default=None,
        help="Restrict to one asset type (default: all).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write the change. Without it the script only reports.",
    )
    args = parser.parse_args()
    asyncio.run(assign(org_id=args.org, apply=args.apply, asset_type=args.asset_type))


if __name__ == "__main__":
    main()
