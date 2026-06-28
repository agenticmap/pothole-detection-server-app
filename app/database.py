"""Database connection pool management using asyncpg."""

import asyncpg

from app.config import settings

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


async def run_migrations(pool: asyncpg.Pool) -> None:
    """Apply SQL migration files in order.

    In production, migrations should be run via a dedicated migration tool
    or CI step. This function is provided for convenience in development.
    """
    import pathlib

    migrations_dir = pathlib.Path(__file__).parent.parent / "migrations"
    if not migrations_dir.exists():
        return

    migration_files = sorted(migrations_dir.glob("*.sql"))
    async with pool.acquire() as conn:
        for migration_file in migration_files:
            sql = migration_file.read_text(encoding="utf-8")
            await conn.execute(sql)
