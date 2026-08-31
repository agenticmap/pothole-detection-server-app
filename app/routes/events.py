"""POST /api/v1/events — Batch sensor event ingestion endpoint.

Wire format contract (frozen at v1):
  - Header: X-Device-Id (required)
  - Header: Accept-Version: v1 (required)
  - Body: { "events": [ {...}, {...}, ... ] } (max 100 per batch)
  - Response: { "accepted": ["id1", ...], "rejected": [{"client_id": "id2", "reason": "..."}] }

Idempotency:
  - INSERT ... ON CONFLICT (client_id) DO NOTHING
  - Both new inserts AND duplicate-skips appear in the `accepted` array
  - The mobile client marks its local Room row `uploaded = 1` for every ID in `accepted`
    (it does NOT delete the row — local History and the map keep working offline)
"""

import logging

from fastapi import APIRouter, HTTPException

from app.config import settings
from app.dependencies import ApiVersion, DbPool, DeviceId
from app.middleware.rate_limit import check_rate_limit
from app.models import EventBatchRequest, EventBatchResponse
from app.services.event_service import insert_events

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1", tags=["events"])


@router.post("/events", response_model=EventBatchResponse)
async def ingest_events(
    body: EventBatchRequest,
    pool: DbPool,
    device_id: DeviceId,
    version: ApiVersion,
):
    """Receive a batch of sensor events from a mobile device.

    Validates each event individually. Valid events are inserted into the
    asset_observation table with ON CONFLICT DO NOTHING for idempotency.
    Invalid events are returned in the rejected array with reasons.
    """
    # Enforce batch size limit
    if len(body.events) > settings.max_batch_size:
        raise HTTPException(
            status_code=400,
            detail=f"Batch size {len(body.events)} exceeds maximum of {settings.max_batch_size}.",
        )

    # Rate limit check (pre-insert to avoid wasted DB work)
    await check_rate_limit(pool, device_id, "events", count=len(body.events))

    # Insert events via service layer
    accepted, rejected = await insert_events(pool, device_id, body.events)

    logger.info(
        "Events ingested: device=%s accepted=%d rejected=%d",
        device_id,
        len(accepted),
        len(rejected),
    )

    return EventBatchResponse(accepted=accepted, rejected=rejected)
