"""FastAPI application factory with lifespan management."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.config import settings
from app.database import close_pool, create_pool, run_migrations, set_pool
from app.fusion.scheduler import start_scheduler, stop_scheduler
from app.routes import auth, events, frames, health, potholes, tiles

# Uvicorn only configures its own `uvicorn.*` loggers, leaving the root logger at
# WARNING — which silently discarded every logger.info() in this app, including
# the "Events ingested" / "Frame ingested" lines needed to watch a real upload.
logging.basicConfig(
    level=getattr(logging, settings.log_level.upper(), logging.INFO),
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage application lifecycle: connection pool and migrations."""
    # Startup
    logger.info("Creating database connection pool...")
    pool = await create_pool()
    set_pool(pool)

    # Run migrations in development mode
    if settings.env == "development":
        logger.info("Running database migrations (development mode)...")
        await run_migrations(pool)

    # Start the in-process fit + fusion scheduler (gated by config).
    start_scheduler(pool)

    logger.info("Server started successfully.")
    yield

    # Shutdown — stop the scheduler (waits for in-flight jobs) before the pool.
    logger.info("Stopping scheduler...")
    stop_scheduler()
    logger.info("Closing database connection pool...")
    await close_pool()
    logger.info("Server shutdown complete.")


app = FastAPI(
    title="Pothole Detection Ingestion Server",
    version="2.0.0",
    description="Receives sensor events and camera frames from mobile devices.",
    lifespan=lifespan,
)

# ── CORS Middleware ───────────────────────────────────────────────────────────
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["X-Request-Id"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
app.include_router(health.router)
app.include_router(events.router)
app.include_router(frames.router)
app.include_router(potholes.router)
app.include_router(auth.router)
app.include_router(auth.well_known_router)
app.include_router(tiles.router)


# ── Global Exception Handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — return a clean 500."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
    )
