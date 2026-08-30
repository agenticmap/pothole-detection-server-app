"""Migrations are re-runnable, and the tables they promise actually arrive.

`run_migrations` is called on every boot in every environment (`app/main.py`) and by
three scripts, and its ledger only protects files it has already seen. A fresh
database applies all of them in one pass, so a migration that is not idempotent
fails in exactly the environment nobody tests: a redeploy onto an existing database
whose ledger was lost. Nothing asserted that before Phase 2.7b.
"""

import pytest

from app.database import run_migrations

pytestmark = pytest.mark.asyncio


async def _column_type(conn, table: str, column: str) -> str | None:
    return await conn.fetchval(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = $2",
        table,
        column,
    )


async def test_running_every_migration_twice_changes_nothing(db_pool):
    """The `db_pool` fixture has already applied them once; this is the second pass.

    A CREATE TABLE without IF NOT EXISTS, or an ALTER that cannot be repeated, raises
    here rather than at deploy time.
    """
    await run_migrations(db_pool)
    await run_migrations(db_pool)


async def test_frame_box_exists_and_stores_corner_origin_boxes(db_pool):
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT to_regclass('frame_box') IS NOT NULL")
        for column in ("frame_client_id", "class_id", "x", "y", "w", "h", "labeled_by"):
            assert await _column_type(conn, "frame_box", column) is not None, column


async def test_frame_label_gained_the_reviewed_marker(db_pool):
    """`boxed_at` is what separates "reviewed and clean" from "never looked at".

    Without it the exporter cannot tell a true background from an unreviewed frame,
    and exporting unreviewed frames as background is the mistake Phase 2.7b exists
    to undo.
    """
    async with db_pool.acquire() as conn:
        assert await _column_type(conn, "frame_label", "boxed_at") == "timestamp with time zone"


async def test_a_box_outside_the_frame_is_rejected(db_pool):
    """Coordinates are normalized 0..1 corner-origin. A pixel value would silently
    train the model on garbage, so the database refuses it."""
    from asyncpg.exceptions import CheckViolationError

    from tests.conftest import insert_frame

    async with db_pool.acquire() as conn:
        await insert_frame(conn, "frame-box-1")
        await conn.execute(
            "INSERT INTO frame_label (frame_client_id, label, labeled_by) VALUES ($1, 0, 'test')",
            "frame-box-1",
        )
        # Valid: a box flush to the bottom-right corner.
        await conn.execute(
            "INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by) "
            "VALUES ($1, 1, 0.5, 0.5, 0.5, 0.5, 'test')",
            "frame-box-1",
        )
        # Invalid: extends past the frame edge.
        with pytest.raises(CheckViolationError):
            await conn.execute(
                "INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by) "
                "VALUES ($1, 1, 0.8, 0.1, 0.5, 0.1, 'test')",
                "frame-box-1",
            )
        # Invalid: pixels, not fractions.
        with pytest.raises(CheckViolationError):
            await conn.execute(
                "INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by) "
                "VALUES ($1, 1, 120.0, 200.0, 40.0, 40.0, 'test')",
                "frame-box-1",
            )


async def test_boxes_die_with_their_frame(db_pool):
    """ON DELETE CASCADE, matching frame_label. An orphaned box would be exported
    against a frame that no longer exists."""
    from tests.conftest import insert_frame

    async with db_pool.acquire() as conn:
        await insert_frame(conn, "frame-box-2")
        await conn.execute(
            "INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by) "
            "VALUES ($1, 0, 0.1, 0.1, 0.2, 0.2, 'test')",
            "frame-box-2",
        )
        await conn.execute("DELETE FROM asset_frame WHERE client_id = $1", "frame-box-2")
        assert await conn.fetchval(
            "SELECT count(*) FROM frame_box WHERE frame_client_id = $1", "frame-box-2"
        ) == 0
