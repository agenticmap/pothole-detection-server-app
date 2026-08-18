/**
 * Viewport statistics client.
 *
 * The KPI card and the chip/legend counts come from SQL rather than from the
 * features MapLibre happens to have drawn. That is not fussiness: rendered tiles
 * carry no per-cluster severity below the aggregate zoom, repeat the same cluster
 * across tile seams because of the tile buffer, and truncate at
 * `tile_max_features` without saying so. See app/services/cluster_stats_service.py.
 */

import { request } from './api.ts';
import { SEVERITY_TIERS } from './severity.ts';

export interface ClusterStats {
  open: number;
  repaired: number;
  unrated: number;
  mean_confidence: number | null;
  repaired_last_30d: number;
  /** Open clusters per tier, positionally parallel to SEVERITY_TIERS. */
  tier_counts: number[];
  generated_at: string;
}

export interface Bbox {
  minLon: number;
  minLat: number;
  maxLon: number;
  maxLat: number;
}

export async function getStats(
  bbox: Bbox,
  assetType: string,
  signal?: AbortSignal,
): Promise<ClusterStats> {
  const params = new URLSearchParams({
    bbox: [bbox.minLon, bbox.minLat, bbox.maxLon, bbox.maxLat].map((n) => n.toFixed(6)).join(','),
    asset_type: assetType,
    // The server has no copy of the ramp — it buckets against whatever floors we
    // send. Derived from SEVERITY_TIERS so the card cannot disagree with the map.
    tiers: SEVERITY_TIERS.map((t) => t.min).join(','),
  });
  const res = await request(`/api/v1/clusters/stats?${params.toString()}`, { signal });
  return (await res.json()) as ClusterStats;
}
