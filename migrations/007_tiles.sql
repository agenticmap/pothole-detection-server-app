-- ============================================================================
-- 007: Indexes for the operator dashboard's vector tiles (Phase 2.5)
-- ============================================================================
-- Additive, idempotent. Indexes only — no schema change. The tile endpoints
-- (app/routes/tiles.py) generate MVT via ST_AsMVT over the existing
-- asset_cluster / asset_observation tables.
--
-- Why a *functional* index and not the existing GiST:
--
--   asset_cluster.centroid is GEOGRAPHY(POINT, 4326) and idx_asset_cluster_centroid
--   (001) is a GiST over that geography. ST_TileEnvelope(z, x, y) returns GEOMETRY
--   in SRID 3857, which is also what ST_AsMVTGeom requires. A predicate of
--   ST_Transform(centroid::geometry, 3857) && ST_TileEnvelope(...) therefore cannot
--   use the geography index, and every tile request degrades to a sequential scan.
--
--   The obvious alternative — transform the envelope back to 4326 and cast it to
--   geography, matching the existing index, as the read path does at
--   cluster_query_service.py — is rejected on purpose: at z0/z1 a tile spans >= 180
--   degrees of longitude and ::geography then raises "Antipodal (180 degrees long)
--   edge detected!". That is the exact failure already worked around for the bbox
--   read path, and there is no reason to re-import it here.
--
--   Indexing the transformed geometry directly keeps the whole tile query planar,
--   removes the antipodal hazard, and matches what ST_AsMVTGeom consumes. Both
--   ST_Transform(geometry, int) and the geography->geometry cast are IMMUTABLE, so
--   they are legal in an index expression.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_asset_cluster_centroid_3857
    ON asset_cluster USING GIST (ST_Transform(centroid::geometry, 3857));

CREATE INDEX IF NOT EXISTS idx_asset_observation_geom_3857
    ON asset_observation USING GIST (ST_Transform(geom::geometry, 3857));

-- Supports the non-spatial half of the tile filter. The spatial predicate is the
-- selective one, so this is a supporting index, not the driver.
CREATE INDEX IF NOT EXISTS idx_asset_cluster_visibility
    ON asset_cluster (asset_type, last_seen)
    WHERE repaired_at IS NULL;

-- The dashboard polls for changed clusters (the deferred-WebSocket substitute).
-- asset_cluster.updated_at already exists and is maintained by the clustering job.
CREATE INDEX IF NOT EXISTS idx_asset_cluster_updated_at
    ON asset_cluster (updated_at);
