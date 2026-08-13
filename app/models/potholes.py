"""Pydantic models for the pothole read endpoints.

Two tiers (Phase 2.4):
  - PUBLIC  GET /api/v1/potholes        → locations only (slim items). Free,
    anonymous. Tells you WHERE confirmed potholes are, not how bad / how
    corroborated.
  - STAFF   GET /api/v1/potholes/detail → full items with severity, confidence,
    distinct_devices, last_seen, source. Requires a staff bearer token.

Both are zoom-aware: high zoom → individual potholes, low zoom → grid-aggregated
clusters.
"""

from typing import Literal

from pydantic import BaseModel, Field

# ── Staff (full detail) ───────────────────────────────────────────────────────


class PotholeItem(BaseModel):
    """An individual confirmed pothole (one asset_cluster row). Returned at high zoom."""

    type: Literal["pothole"] = "pothole"
    id: str = Field(..., description="cluster_id of the confirmed pothole.")
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)
    severity: float | None = None
    confidence: float | None = None
    observation_count: int = Field(..., ge=0)
    distinct_devices: int = Field(..., ge=0)
    last_seen: str | None = Field(default=None, description="ISO-8601 timestamp.")
    source: str | None = None


class ClusterAggItem(BaseModel):
    """A grid-aggregated group of nearby potholes. Returned at low zoom."""

    type: Literal["cluster"] = "cluster"
    centroid_lat: float = Field(..., ge=-90.0, le=90.0)
    centroid_lon: float = Field(..., ge=-180.0, le=180.0)
    count: int = Field(..., ge=1, description="Number of confirmed potholes in the cell.")
    max_severity: float | None = None


class PotholesResponse(BaseModel):
    """GET /api/v1/potholes/detail response (staff)."""

    items: list[PotholeItem | ClusterAggItem] = Field(default_factory=list)
    generated_at: str = Field(..., description="Server time the response was produced (ISO-8601).")
    next_since: str | None = Field(
        default=None,
        description="Pass back as ?since= on the next poll for incremental fetch.",
    )


# ── Public (locations only) ───────────────────────────────────────────────────


class PublicPotholeItem(BaseModel):
    """A confirmed pothole's LOCATION only — no severity/confidence/corroboration."""

    type: Literal["pothole"] = "pothole"
    id: str = Field(..., description="cluster_id of the confirmed pothole.")
    lat: float = Field(..., ge=-90.0, le=90.0)
    lon: float = Field(..., ge=-180.0, le=180.0)


class PublicClusterItem(BaseModel):
    """A grid-aggregated group's location + count only — no max severity."""

    type: Literal["cluster"] = "cluster"
    centroid_lat: float = Field(..., ge=-90.0, le=90.0)
    centroid_lon: float = Field(..., ge=-180.0, le=180.0)
    count: int = Field(..., ge=1, description="Number of confirmed potholes in the cell.")


class PublicPotholesResponse(BaseModel):
    """GET /api/v1/potholes response (public, locations only)."""

    items: list[PublicPotholeItem | PublicClusterItem] = Field(default_factory=list)
    generated_at: str = Field(..., description="Server time the response was produced (ISO-8601).")
    next_since: str | None = Field(
        default=None,
        description="Pass back as ?since= on the next poll for incremental fetch.",
    )
