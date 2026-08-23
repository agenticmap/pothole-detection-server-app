"""Database connection pool management using asyncpg."""

import logging

import asyncpg

from app.config import settings

logger = logging.getLogger(__name__)

# Module-level pool reference, initialized during app lifespan
_pool: asyncpg.Pool | None = None


async def create_pool() -> asyncpg.Pool:
    """Create and return an asyncpg connection pool.

    Configures the pool for compatibility with Supabase's pgbouncer
    (transaction-mode pooling) when DATABASE_USE_POOLER is true.
    """
    connect_kwargs: dict = {}

    # When connecting through Supabase's connection pooler (pgbouncer in
    # transaction mode), prepared statements are not supported. Disable
    # the statement cache to avoid "prepared statement does not exist" errors.
    if settings.database_use_pooler:
        connect_kwargs["statement_cache_size"] = 0

    pool = await asyncpg.create_pool(
        dsn=settings.database_url,
        min_size=settings.database_min_connections,
        max_size=settings.database_max_connections,
        command_timeout=30,
        **connect_kwargs,
    )

    # Verify connectivity
    async with pool.acquire() as conn:
        await conn.execute("SELECT 1")

    return pool


async def close_pool() -> None:
    """Close the connection pool gracefully."""
    global _pool
    if _pool is not None:
        await _pool.close()
        _pool = None


def get_pool() -> asyncpg.Pool:
    """Return the active connection pool. Raises if not initialized."""
    if _pool is None:
        raise RuntimeError("Database pool is not initialized. Is the app running?")
    return _pool


def set_pool(pool: asyncpg.Pool) -> None:
    """Set the module-level pool reference (called during app startup)."""
    global _pool
    _pool = pool


# Advisory lock held while migrating, so `uvicorn --workers N` cannot have two
# processes applying the same file concurrently. Distinct from the job locks in
# app/fusion/service.py (0x504F54/55/57) and app/detection/service.py (0x504F56).
_MIGRATION_LOCK_KEY = 0x504F53  # 'POT' - 1

_MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    filename    TEXT PRIMARY KEY,
    checksum    TEXT NOT NULL,
    applied_at  TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply any SQL migration files that have not been applied yet.

    Tracked in `schema_migrations`, so each file runs exactly once and this is
    safe to call on every boot in any environment. Previously this re-executed
    all files on every development start and was skipped entirely outside
    development -- which meant starting with ENV=production against a fresh
    database created no tables at all.

    Upgrading an existing database is a no-op in effect: the ledger starts empty,
    so all files are re-applied once, and every one of them is written to be
    idempotent (CREATE ... IF NOT EXISTS / ON CONFLICT DO NOTHING). That is what
    made the old re-run-everything behaviour survivable in the first place.
    """
    import hashlib
    import pathlib

    migrations_dir = pathlib.Path(__file__).parent.parent / "migrations"
    if not migrations_dir.exists():
        logger.warning("No migrations directory at %s; skipping.", migrations_dir)
        return

    migration_files = sorted(migrations_dir.glob("*.sql"))
    if not migration_files:
        logger.warning("No .sql files in %s; skipping.", migrations_dir)
        return

    async with pool.acquire() as conn:
        # Blocking (not `try`) lock: a second worker should wait and then find
        # there is nothing left to do, rather than race ahead onto a half-built
        # schema.
        await conn.execute("SELECT pg_advisory_lock($1)", _MIGRATION_LOCK_KEY)
        try:
            await conn.execute(_MIGRATION_TABLE_SQL)
            applied = {
                r["filename"]: r["checksum"]
                for r in await conn.fetch("SELECT filename, checksum FROM schema_migrations")
            }

            pending = 0
            for migration_file in migration_files:
                sql = migration_file.read_text(encoding="utf-8")
                checksum = hashlib.sha256(sql.encode("utf-8")).hexdigest()
                name = migration_file.name

                if name in applied:
                    if applied[name] != checksum:
                        # Editing an applied migration is a mistake, but failing
                        # to boot over it is worse than saying so loudly.
                        logger.warning(
                            "Migration %s changed since it was applied "
                            "(recorded %s, now %s); not re-applying.",
                            name, applied[name][:12], checksum[:12],
                        )
                    continue

                # One transaction per file: a failure leaves no ledger row, so
                # the next boot retries this file rather than skipping it.
                async with conn.transaction():
                    await conn.execute(sql)
                    await conn.execute(
                        "INSERT INTO schema_migrations (filename, checksum) VALUES ($1, $2)",
                        name, checksum,
                    )
                logger.info("Applied migration %s", name)
                pending += 1

            if pending:
                logger.info("Applied %d migration(s).", pending)
            else:
                logger.info("Schema up to date (%d migration(s) already applied).", len(applied))
        finally:
            await conn.execute("SELECT pg_advisory_unlock($1)", _MIGRATION_LOCK_KEY)
