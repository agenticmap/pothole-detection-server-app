-- ============================================================================
-- Migration 016 — make device_rate_limit usable
-- ============================================================================
-- The table has existed since migration 001 and was never written to: the live
-- limiter kept module-level dicts, so with `uvicorn --workers 2` each worker
-- enforced its own private ceiling and the effective limit was doubled.
--
-- The limiter now reads and writes this table. Two access patterns need support:
--
--   * per-request: sum a device's buckets inside the trailing hour. The primary
--     key (device_id, resource, window_start) already leads with the right
--     columns, so that one is covered.
--   * pruning: delete every bucket older than the window, across all devices.
--     That is a full scan without an index on window_start alone.
-- ============================================================================

CREATE INDEX IF NOT EXISTS idx_device_rate_limit_window
    ON device_rate_limit (window_start);

COMMENT ON TABLE device_rate_limit IS
    'Per-device, per-resource request counts in one-minute buckets. Summed over '
    'the trailing hour by app/middleware/rate_limit.py. Pruned by the retention '
    'job, not on the request path.';
