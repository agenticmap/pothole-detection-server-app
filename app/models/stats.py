"""Response model for the dashboard's viewport statistics (Phase 2.5b)."""

from datetime import datetime

from pydantic import BaseModel, Field


class ClusterStatsResponse(BaseModel):
    """Counts for the clusters visible in one viewport.

    `tier_counts` is positional and parallel to the `tiers` floors the caller
    passed in — the dashboard owns the severity ramp (dashboard/src/severity.ts)
    and there is deliberately no second copy of those boundaries on the server to
    drift out of step with it.
    """

    open: int = Field(description="Unrepaired clusters in the viewport.")
    repaired: int = Field(description="Repaired clusters in the viewport.")
    unrated: int = Field(description="Open clusters with no severity score yet.")
    corroborated: int = Field(
        default=0,
        description=(
            "Open clusters that meet the PUBLICATION rule -- seen by enough distinct "
            "devices or on enough distinct passes to be served by /api/v1/potholes. "
            "Forming a cluster needs one reading; being publishable needs corroboration, "
            "and the console showed only the first number."
        ),
    )
    mean_confidence: float | None = Field(
        default=None, description="Mean fused confidence across open clusters; null if none."
    )
    repaired_last_30d: int = Field(
        description="Clusters in the viewport with a 'repaired' log entry in the last 30 days."
    )
    tier_counts: list[int] = Field(
        description="Open cluster counts per severity tier, in the order the tiers were given."
    )
    source_counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Open cluster counts per detection source. A source with no clusters in the "
            "viewport is omitted rather than reported as zero."
        ),
    )
    generated_at: datetime
