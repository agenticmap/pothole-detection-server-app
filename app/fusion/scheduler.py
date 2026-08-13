"""APScheduler wiring — in-process fit + fusion jobs on the app's asyncio loop.

Started/stopped from the FastAPI lifespan. Both jobs are single-instance
(`max_instances=1`) and coalesce missed ticks, so a slow run never overlaps the
next. Disabled hermetically in tests via FUSION_ENABLED / SENSOR_FIT_ENABLED.
"""

from __future__ import annotations

import logging

import asyncpg
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from app.config import settings
from app.detection.service import run_detection_job
from app.fusion.service import run_cluster_job, run_fit_job, run_fusion_job

logger = logging.getLogger(__name__)

_scheduler: AsyncIOScheduler | None = None


def start_scheduler(pool: asyncpg.Pool) -> None:
    """Create and start the scheduler with the fit and fusion jobs (if enabled)."""
    global _scheduler
    if _scheduler is not None:
        return
    if not (
        settings.fusion_enabled
        or settings.sensor_fit_enabled
        or settings.clustering_enabled
        or settings.detection_enabled
    ):
        logger.info("Fusion + fit + clustering + detection jobs disabled; scheduler not started.")
        return

    scheduler = AsyncIOScheduler(timezone="UTC")

    if settings.sensor_fit_enabled:
        scheduler.add_job(
            run_fit_job,
            trigger=IntervalTrigger(minutes=settings.sensor_fit_interval_minutes),
            args=[pool],
            id="sensor_fit",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )

    if settings.fusion_enabled:
        scheduler.add_job(
            run_fusion_job,
            trigger=IntervalTrigger(minutes=settings.fusion_interval_minutes),
            args=[pool],
            id="fusion",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    if settings.clustering_enabled:
        scheduler.add_job(
            run_cluster_job,
            trigger=IntervalTrigger(minutes=settings.clustering_interval_minutes),
            args=[pool],
            id="cluster",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
        )

    if settings.detection_enabled:
        scheduler.add_job(
            run_detection_job,
            trigger=IntervalTrigger(minutes=settings.detection_interval_minutes),
            args=[pool],
            id="detection",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=60,
        )

    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "Scheduler started (fit=%s, fusion=%s, clustering=%s, detection=%s).",
        settings.sensor_fit_enabled, settings.fusion_enabled,
        settings.clustering_enabled, settings.detection_enabled,
    )


def stop_scheduler() -> None:
    """Shut the scheduler down, waiting for any in-flight job to finish."""
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=True)
        _scheduler = None
        logger.info("Scheduler stopped.")
