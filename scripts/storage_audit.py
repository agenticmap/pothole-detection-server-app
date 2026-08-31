"""Reconcile the frame store against asset_frame — read-only unless told otherwise.

`storage/frames/` and the `asset_frame` table drift apart in both directions and
nothing has ever checked:

  * **Orphan files** — a JPEG on disk with no row. `store_frame` writes the file
    *after* the row commits, so a crash between the two leaves a file behind;
    `phase-2.7-detection-enablement.md` recorded 59 of these.
  * **Dangling rows** — a row whose `jpeg_url` points at nothing. Worse than an
    orphan: the frame appears in a cluster's evidence list and then 404s when an
    operator clicks it.
  * **Demo artefacts** — `scripts/seed_demo.py` refuses to write to any database
    but `pothole_test`/`pothole_ci`, but its JPEGs go to `STORAGE_LOCAL_PATH`,
    which is shared. So seeding a demo leaves `demo-dev-*` directories sitting in
    the real frame store, where they inflate every per-device storage figure and
    any frame count taken off the filesystem.

Both counts feed the retention work: a per-device storage budget computed over a
store that is a third demo data is measuring the wrong thing.

## The guard, and why it points the way it does

`--delete-orphans` decides what to delete by asking the database what it knows
about. Pointed at a database that has been TRUNCATEd -- which is exactly what
`pothole_test` is between test runs -- every real frame looks like an orphan and
the flag would delete the entire collected archive.

So deletion refuses to run against `pothole_test`/`pothole_ci`. That is the same
direction as `label_frames.py` and the OPPOSITE of `seed_demo.py`: seed_demo
writes fabricated data so it must only touch a scratch database, while this reads
real data to decide what to destroy, so it must never trust a scratch one.

Usage (from the repo root):

    python scripts/storage_audit.py                  # report only
    python scripts/storage_audit.py --delete-orphans # remove orphan files
    python scripts/storage_audit.py --delete-demo    # remove demo-dev-* trees
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path
from urllib.parse import urlsplit

# Run directly (`python scripts/storage_audit.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool  # noqa: E402

# Databases the fixtures TRUNCATE. Deleting files based on what one of these
# "knows about" would delete everything.
_SCRATCH_DATABASES = frozenset({"pothole_test", "pothole_ci"})

# seed_demo.py tags every artefact it creates with this device prefix.
_DEMO_PREFIX = "demo-dev-"


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _human(n_bytes: int) -> str:
    value = float(n_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GB"


def _scan_disk(root: Path) -> dict[str, int]:
    """Every stored file, keyed by its path relative to the storage root.

    The key matches what `_store_jpeg_local` writes into `asset_frame.jpeg_url`,
    with separators normalised so a Windows scan compares against a POSIX-style
    column.
    """
    if not root.is_dir():
        return {}
    found: dict[str, int] = {}
    for path in root.rglob("*"):
        if path.is_file():
            found[path.relative_to(root).as_posix()] = path.stat().st_size
    return found


async def _load_rows(pool) -> dict[str, str]:
    """jpeg_url -> client_id for every frame row."""
    async with pool.acquire() as conn:
        rows = await conn.fetch("SELECT client_id, jpeg_url FROM asset_frame")
    return {r["jpeg_url"]: r["client_id"] for r in rows if r["jpeg_url"]}


def _report(disk: dict[str, int], rows: dict[str, str], root: Path) -> tuple[list[str], list[str]]:
    referenced = set(rows)
    on_disk = set(disk)

    orphans = sorted(on_disk - referenced)
    dangling = sorted(referenced - on_disk)
    demo = sorted(p for p in on_disk if p.startswith(_DEMO_PREFIX))
    # A demo file is an orphan too when the demo rows live in another database,
    # which is the normal case. Report it once, under the more specific heading.
    orphans_excl_demo = [p for p in orphans if not p.startswith(_DEMO_PREFIX)]

    total_bytes = sum(disk.values())
    print(f"Storage root : {root}")
    print(f"Database     : {_database_name(settings.database_url)}")
    print()
    print(f"Files on disk      : {len(on_disk):>6}  ({_human(total_bytes)})")
    print(f"Rows in asset_frame: {len(rows):>6}")
    print()
    print(
        f"Orphan files (no row)     : {len(orphans_excl_demo):>6}  "
        f"({_human(sum(disk[p] for p in orphans_excl_demo))})"
    )
    print(
        f"Demo artefacts ({_DEMO_PREFIX}*) : {len(demo):>6}  "
        f"({_human(sum(disk[p] for p in demo))})"
    )
    print(f"Dangling rows (no file)   : {len(dangling):>6}")

    if dangling:
        print()
        print("  Dangling rows serve a 404 from the cluster detail panel. Sample:")
        for url in dangling[:5]:
            print(f"    {rows[url]}  ->  {url}")
        if len(dangling) > 5:
            print(f"    ... and {len(dangling) - 5} more")
        print("  These cannot be fixed here -- the bytes are gone. Either restore them")
        print("  from a backup or NULL the jpeg_url so the panel stops offering them.")

    return orphans_excl_demo, demo


def _delete(paths: list[str], root: Path, label: str) -> None:
    if not paths:
        print(f"Nothing to delete for {label}.")
        return
    freed = 0
    for rel in paths:
        target = root / rel
        try:
            freed += target.stat().st_size
            target.unlink()
        except OSError as e:  # noqa: PERF203 - reporting each failure is the point
            print(f"  could not delete {rel}: {e}")
    # Prune directories the deletions emptied, so the store does not accumulate
    # empty per-device trees.
    for directory in sorted(
        {p for p in root.rglob("*") if p.is_dir()}, key=lambda p: len(p.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass  # not empty, which is the normal case
    print(f"Deleted {len(paths)} {label} file(s), freed {_human(freed)}.")


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument(
        "--delete-orphans", action="store_true", help="Delete files with no asset_frame row"
    )
    ap.add_argument(
        "--delete-demo", action="store_true", help=f"Delete {_DEMO_PREFIX}* files from seed_demo.py"
    )
    args = ap.parse_args()

    db_name = _database_name(settings.database_url)
    destructive = args.delete_orphans or args.delete_demo
    if destructive and db_name in _SCRATCH_DATABASES:
        ap.error(
            f"Refusing to delete files while DATABASE_URL points at {db_name!r}. "
            f"The test fixtures TRUNCATE it, so every real frame would look like an "
            f"orphan and be destroyed. Point DATABASE_URL at the database that owns "
            f"these files."
        )

    root = Path(settings.storage_local_path).resolve()
    disk = _scan_disk(root)

    pool = await create_pool()
    try:
        rows = await _load_rows(pool)
    finally:
        await pool.close()

    orphans, demo = _report(disk, rows, root)

    if not destructive:
        print()
        print("Read-only. Re-run with --delete-orphans and/or --delete-demo to act.")
        return

    print()
    if args.delete_orphans:
        _delete(orphans, root, "orphan")
    if args.delete_demo:
        _delete(demo, root, "demo")


if __name__ == "__main__":
    asyncio.run(main())
