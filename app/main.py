"""FastAPI application factory with lifespan management."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import settings
from app.database import close_pool, create_pool, run_migrations, set_pool
from app.fusion.scheduler import start_scheduler, stop_scheduler
from app.routes import auth, clusters, events, frames, health, potholes, tiles

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

    # Migrations are tracked in schema_migrations and applied at most once, so
    # this runs in every environment. It used to be gated on
    # env == "development", which meant a production boot against a fresh
    # database created no tables at all and then served 500s.
    logger.info("Applying database migrations...")
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
app.include_router(clusters.router)


# ── Operator dashboard (Phase 2.5) ────────────────────────────────────────────
# Mounted AFTER the routers: Starlette matches in registration order, so the API
# always wins. A prefixed mount rather than "/" so there is no chance of shadowing
# /api/v1/*, /health or /.well-known/jwks.json.
#
# The isdir() guard is load-bearing, not defensive habit: StaticFiles raises
# RuntimeError from its constructor when the directory is missing, and this module
# is imported by the test suite — an unbuilt frontend would fail all tests and stop
# the container booting. A server without a dashboard should simply not serve one.
_dashboard_dist = Path(settings.dashboard_dist_path)
if not _dashboard_dist.is_absolute():
    # Relative to the repo root, not the CWD — uvicorn is not always launched there.
    _dashboard_dist = Path(__file__).resolve().parent.parent / _dashboard_dist

if _dashboard_dist.is_dir():
    app.mount(
        "/dashboard",
        StaticFiles(directory=str(_dashboard_dist), html=True),
        name="dashboard",
    )
    logger.info("Operator dashboard mounted at /dashboard from %s", _dashboard_dist)
else:
    logger.info(
        "No dashboard bundle at %s — /dashboard not mounted. "
        "Build it with: cd dashboard && npm install && npm run build",
        _dashboard_dist,
    )


# ── Basemap archive (Phase 2.5b) ──────────────────────────────────────────────
# One PMTiles file served over HTTP range requests — there is no basemap tile
# server. Starlette's FileResponse implements Range, which is the whole
# mechanism: without it a client would pull the entire archive to read one tile.
#
# Deliberately unauthenticated. It is public OpenStreetMap data, and the pmtiles
# protocol handler owns its own fetch, so the dashboard's transformRequest never
# sees these requests and could not attach a bearer to them anyway.
#
# Same load-bearing isdir() guard as the dashboard mount above.
_basemap_dir = Path(settings.basemap_path)
if not _basemap_dir.is_absolute():
    _basemap_dir = Path(__file__).resolve().parent.parent / _basemap_dir

if _basemap_dir.is_dir():
    app.mount("/basemap", StaticFiles(directory=str(_basemap_dir)), name="basemap")
    logger.info("Basemap archive mounted at /basemap from %s", _basemap_dir)
else:
    logger.info(
        "No basemap archive at %s — /basemap not mounted, the map will have no "
        "background. See dashboard/README.md to generate one.",
        _basemap_dir,
    )


# ── Global Exception Handler ──────────────────────────────────────────────────
@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    """Catch-all for unhandled exceptions — return a clean 500."""
    logger.exception("Unhandled exception on %s %s", request.method, request.url.path)
    return JSONResponse(
        status_code=500,
        content={"error": "internal_server_error", "detail": "An unexpected error occurred."},
    )
