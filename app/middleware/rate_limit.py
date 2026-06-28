"""In-memory sliding-window rate limiter per device.

This implementation uses a simple time-bucketed counter stored in a dict.
It is suitable for a single-instance deployment. For horizontal scaling,
replace with a Redis-backed implementation (e.g., redis INCR with TTL).

Rate limits (from enterprise-architecture-plan.md §2.8 / roadmap §2.8):
  - 100 events/hour per device
  - 100 frames/hour per device
  - HTTP 429 on excess
"""

import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException

from app.config import settings

# Storage: { device_id: [(timestamp_seconds, count), ...] }
# We use a simple 1-minute bucket approach for memory efficiency.
_BUCKET_SECONDS = 60  # 1-minute buckets
_WINDOW_SECONDS = 3600  # 1-hour sliding window

_event_buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
_frame_buckets: dict[str, list[tuple[float, int]]] = defaultdict(list)
_lock = Lock()


def _get_current_count(buckets: list[tuple[float, int]], now: float) -> int:
    """Sum counts in buckets within the sliding window."""
    cutoff = now - _WINDOW_SECONDS
    return sum(count for ts, count in buckets if ts >= cutoff)


def _prune_and_increment(
    bucket_store: dict[str, list[tuple[float, int]]], device_id: str, increment: int
) -> int:
    """Prune old buckets, add increment to current bucket, return total in window."""
    now = time.time()
    cutoff = now - _WINDOW_SECONDS
    current_bucket_ts = int(now / _BUCKET_SECONDS) * _BUCKET_SECONDS

    buckets = bucket_store[device_id]

    # Prune expired buckets
    bucket_store[device_id] = [(ts, c) for ts, c in buckets if ts >= cutoff]
    buckets = bucket_store[device_id]

    # Find or create current bucket
    if buckets and buckets[-1][0] == current_bucket_ts:
        buckets[-1] = (current_bucket_ts, buckets[-1][1] + increment)
    else:
        buckets.append((current_bucket_ts, increment))

    return _get_current_count(buckets, now)


def check_rate_limit(device_id: str, resource: str, count: int = 1) -> None:
    """Check and enforce rate limit for a device.

    Args:
        device_id: The X-Device-Id header value.
        resource: Either "events" or "frames".
        count: Number of items in this request (batch size for events).

    Raises:
        HTTPException: 429 if the device has exceeded its hourly limit.
    """
    if resource == "events":
        limit = settings.rate_limit_events_per_hour
        store = _event_buckets
    elif resource == "frames":
        limit = settings.rate_limit_frames_per_hour
        store = _frame_buckets
    else:
        return

    with _lock:
        current_total = _prune_and_increment(store, device_id, count)

    if current_total > limit:
        raise HTTPException(
            status_code=429,
            detail={
                "error": "rate_limit_exceeded",
                "device_id": device_id,
                "resource": resource,
                "limit": limit,
                "window": "1 hour",
                "current": current_total,
            },
        )


def reset_rate_limits() -> None:
    """Clear all rate limit state. Used in tests."""
    with _lock:
        _event_buckets.clear()
        _frame_buckets.clear()
