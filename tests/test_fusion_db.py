"""Integration tests for the fit + fusion jobs (require a local Postgres+PostGIS).

Skipped automatically by the db_pool fixture when no database is reachable.
"""

import numpy as np
import pytest

from app.config import settings
from app.fusion.service import run_fit_job, run_fusion_job
from app.sensor_model.store import load_active_model
from tests.conftest import insert_frame, insert_observation

pytestmark = pytest.mark.asyncio


async def _seed_three_blobs(conn, n_per_blob: int, prefix: str):
    """Insert 3*n_per_blob observations forming 3 separated [ratio, gbar] blobs."""
    rng = np.random.default_rng(0)
    i = 0
    for rc, gc in [(1.0, 1.0), (4.0, 4.0), (8.0, 8.0)]:
        for _ in range(n_per_blob):
            ratio = max(0.1, float(rng.normal(rc, 0.3)))
            gbar = max(0.1, float(rng.normal(gc, 0.3)))
            await insert_observation(
                conn, f"{prefix}-{i}", device_id="dev-1",
                magnitude=ratio, accel_std=1.0, gbar_in_max=gbar, speed_mps=12.0,
            )
            i += 1


async def test_fit_job_gate_and_activation(db_pool):
    # Below the gate (default N_min=200) -> no model.
    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=40, prefix="a")  # 120 rows < 200
    assert await run_fit_job(db_pool) is None
    assert await load_active_model(db_pool) is None

    # Add fresh rows to cross the gate -> a model is fit and activated.
    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=50, prefix="b")  # +150 -> 270 >= 200
    version = await run_fit_job(db_pool)
    assert version is not None
    model = await load_active_model(db_pool)
    assert model is not None
    assert model.k == 3
    assert model.iforest is not None


async def test_fusion_pairs_nearest_observation(db_pool):
    async with db_pool.acquire() as conn:
        # One frame, two candidate observations: one inside the window, one outside.
        await insert_frame(
            conn, "frame-1", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00", lat=43.6532, lon=-79.3832,
            device_probability=0.8,
        )
        # In-window: same spot, ~0.5s earlier.
        await insert_observation(
            conn, "obs-near", device_id="dev-1",
            ts="2026-05-27T10:29:59.5+00:00", lat=43.6532, lon=-79.3832,
            magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0,
        )
        # Out-of-window: ~10s away in time.
        await insert_observation(
            conn, "obs-far", device_id="dev-1",
            ts="2026-05-27T10:30:10+00:00", lat=43.6532, lon=-79.3832,
            magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0,
        )

    # No active model -> heuristic fallback engine, but pairing still happens.
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='frame-1'"
        )
        run = await conn.fetchrow("SELECT inputs_count, outputs_count FROM fusion_run")

    assert len(pairs) == 1
    assert pairs[0]["event_client_id"] == "obs-near"
    assert pairs[0]["frame_client_id"] == "frame-1"
    assert 0.0 <= pairs[0]["fused_confidence"] <= 1.0
    assert abs(pairs[0]["delta_ms"]) < 3000
    assert frame["processed_at"] is not None
    assert run["inputs_count"] == 1 and run["outputs_count"] == 1


async def test_an_unpaired_frame_is_held_inside_the_retry_grace(db_pool):
    """Phase 2.2d: don't retire a frame whose event may still be uploading.

    Observations and frames drain from separate upload queues hours apart, so a
    frame processed the instant it arrives can be permanently orphaned. Before
    2.2d the whole batch was marked regardless of outcome.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "lonely-frame", device_id="dev-1")
        # An observation on a DIFFERENT device must not pair.
        await insert_observation(
            conn, "other-dev-obs", device_id="dev-2",
            ts="2026-05-27T10:30:00+00:00", magnitude=6.0, accel_std=1.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='lonely-frame'"
        )
    assert len(pairs) == 0
    # received_at defaults to now(), so the frame is inside the 30-minute grace.
    assert frame["processed_at"] is None


async def test_an_unpaired_frame_is_retired_once_the_grace_expires(db_pool):
    """The other half: the grace bounds the retry, it does not disable it."""
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "old-lonely-frame", device_id="dev-1")
        # Age the frame past fusion_retry_grace_minutes (default 30).
        await conn.execute(
            "UPDATE asset_frame SET received_at = now() - interval '2 hours' "
            "WHERE client_id = 'old-lonely-frame'"
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='old-lonely-frame'"
        )
    assert frame["processed_at"] is not None


async def test_a_paired_frame_is_retired_immediately(db_pool):
    """Pairing is the normal exit; the grace must not delay it."""
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "paired-frame", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00")
        await insert_observation(
            conn, "its-obs", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00", magnitude=6.0, accel_std=1.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        frame = await conn.fetchrow(
            "SELECT processed_at FROM asset_frame WHERE client_id='paired-frame'"
        )
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert len(pairs) == 1
    assert frame["processed_at"] is not None


async def test_fusion_is_idempotent_and_deterministic(db_pool):
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f1", device_id="dev-1", device_probability=0.7)
        await insert_observation(
            conn, "o1", device_id="dev-1",
            ts="2026-05-27T10:29:59.7+00:00", magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        first = await conn.fetchrow(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='f1'"
        )
        # Reset the frame so it re-pairs; confidence must be byte-identical.
        await conn.execute("UPDATE asset_frame SET processed_at = NULL WHERE client_id='f1'")
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='f1'"
        )
    assert len(pairs) == 1  # upsert, not duplicate
    assert pairs[0]["fused_confidence"] == first["fused_confidence"]


async def test_fusion_prefers_server_probability_over_device(db_pool):
    # Phase 2.3: when a frame has a server_probability, fusion uses it (COALESCE)
    # over the weaker on-device probability.
    async with db_pool.acquire() as conn:
        # Frame A: low device prob (0.1) but high SERVER prob (0.9).
        await insert_frame(
            conn, "fa", device_id="dva", ts="2026-05-27T10:30:00+00:00",
            lat=43.10, lon=-79.10, device_probability=0.1,
        )
        await conn.execute("UPDATE asset_frame SET server_probability=0.9 WHERE client_id='fa'")
        await insert_observation(
            conn, "oa", device_id="dva", ts="2026-05-27T10:29:59.7+00:00",
            lat=43.10, lon=-79.10, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )
        # Frame B: same setup but only device prob (0.1), no server prob.
        await insert_frame(
            conn, "fb", device_id="dvb", ts="2026-05-27T10:30:00+00:00",
            lat=43.20, lon=-79.20, device_probability=0.1,
        )
        await insert_observation(
            conn, "ob", device_id="dvb", ts="2026-05-27T10:29:59.7+00:00",
            lat=43.20, lon=-79.20, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )

    await run_fusion_job(db_pool)

    sql = "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id=$1"
    async with db_pool.acquire() as conn:
        fa = await conn.fetchval(sql, "fa")
        fb = await conn.fetchval(sql, "fb")

    assert fa is not None and fb is not None
    assert fa > fb  # identical sensor signal; A's stronger visual term wins


async def test_a_detector_that_found_nothing_does_not_veto_the_sensor(db_pool):
    """Phase 2.7: server_probability = 0.0 must mean "no evidence", not "clean road".

    onnx_v1 reports 0.0 when no box clears the confidence threshold, and `logit`
    clamps 0.0 to 1e-6 = -13.8155. Fed into the blend that is near-certain evidence
    AGAINST a pothole, so a confident sensor event fuses to ~0.003 -- the camera
    silently overrules the accelerometer on 81% of real frames. Finding no box is a
    missing modality (the ROI may hold no road), so it falls back to the device
    probability, exactly as a detection *failure* already does.
    """
    async with db_pool.acquire() as conn:
        # Frame A: detector ran and found nothing (0.0), device saw 0.6.
        await insert_frame(
            conn, "fz", device_id="dvz", ts="2026-05-27T10:30:00+00:00",
            lat=43.40, lon=-79.40, device_probability=0.6,
        )
        await conn.execute("UPDATE asset_frame SET server_probability=0.0 WHERE client_id='fz'")
        await insert_observation(
            conn, "oz", device_id="dvz", ts="2026-05-27T10:29:59.7+00:00",
            lat=43.40, lon=-79.40, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )
        # Frame B: identical, but the detector never ran (NULL) -- the documented
        # fallback path. A and B must agree, because they carry the same evidence.
        await insert_frame(
            conn, "fy", device_id="dvy", ts="2026-05-27T10:30:00+00:00",
            lat=43.50, lon=-79.50, device_probability=0.6,
        )
        await insert_observation(
            conn, "oy", device_id="dvy", ts="2026-05-27T10:29:59.7+00:00",
            lat=43.50, lon=-79.50, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )

    await run_fusion_job(db_pool)

    sql = "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id=$1"
    async with db_pool.acquire() as conn:
        fz = await conn.fetchval(sql, "fz")
        fy = await conn.fetchval(sql, "fy")

    assert fz is not None and fy is not None
    # Same evidence, same verdict: "scored, found nothing" == "not scored".
    assert fz == pytest.approx(fy)
    # And crucially not the collapsed value a 0.0 visual term would produce.
    assert fz > 0.1, f"a silent detector vetoed the sensor: fused={fz}"


async def test_a_real_detection_of_zero_is_still_distinguishable_in_the_column(db_pool):
    """The 0.0 stays stored; only its *reading* by the blend changes.

    Fusion must not rewrite asset_frame. `count(server_probability)` is how the
    runbook measures backfill progress, so turning the 0.0 into a NULL at write time
    would make "scored, found nothing" indistinguishable from "never scored".
    """
    async with db_pool.acquire() as conn:
        await insert_frame(
            conn, "fx", device_id="dvx", ts="2026-05-27T10:30:00+00:00",
            lat=43.60, lon=-79.60, device_probability=0.6,
        )
        await conn.execute("UPDATE asset_frame SET server_probability=0.0 WHERE client_id='fx'")
        await insert_observation(
            conn, "ox", device_id="dvx", ts="2026-05-27T10:29:59.7+00:00",
            lat=43.60, lon=-79.60, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )

    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        stored = await conn.fetchval(
            "SELECT server_probability FROM asset_frame WHERE client_id='fx'"
        )
    assert stored == 0.0


async def test_fit_job_is_single_flight_under_advisory_lock(db_pool):
    """Regression: run_fit_job had no advisory lock while every other job did.

    The Dockerfile runs `uvicorn --workers 2`, so two schedulers can tick at the
    same moment. Both would fit and both would call save_model, racing on the
    idx_sensor_model_active partial unique index. Here the lock is taken out from
    under the job to prove it declines to run rather than proceeding.
    """
    from app.fusion.service import _FIT_LOCK_KEY

    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=90, prefix="lock")  # 270 >= 200

    # Hold the lock on a connection the job cannot use, mimicking the other worker.
    async with db_pool.acquire() as holder:
        held = await holder.fetchval("SELECT pg_try_advisory_lock($1)", _FIT_LOCK_KEY)
        assert held is True
        try:
            assert await run_fit_job(db_pool) is None
            assert await load_active_model(db_pool) is None
        finally:
            await holder.execute("SELECT pg_advisory_unlock($1)", _FIT_LOCK_KEY)

    # Lock released -> the same call now does the work, so the skip above was the
    # lock and not the gate.
    assert await run_fit_job(db_pool) is not None
    assert await load_active_model(db_pool) is not None

    # And the lock is not leaked: a third caller can still take it.
    async with db_pool.acquire() as conn:
        assert await conn.fetchval("SELECT pg_try_advisory_lock($1)", _FIT_LOCK_KEY) is True
        await conn.execute("SELECT pg_advisory_unlock($1)", _FIT_LOCK_KEY)


async def test_detection_backfill_can_rescore_existing_pairs(db_pool):
    """Phase 2.7: the re-fuse path scripts/backfill_detection.py relies on.

    Fusion runs long before server-side detection is enabled, so pairs get a
    fused_confidence derived from the weak device probability and — because fusion
    only ever looks at frames WHERE processed_at IS NULL — would keep it forever.
    docs/phases/phase-2.3-detection-plan.md lists this as out of scope ("re-fusing frames
    detected after they were already paired"); the backfill closes it by clearing
    processed_at, which works only because _UPSERT_PAIR_SQL upserts on
    (event_client_id, frame_client_id).

    Asserted here rather than in the script so the guarantee survives a refactor of
    either side.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(
            conn, "fr1", device_id="dv1", ts="2026-05-27T10:30:00+00:00",
            lat=43.30, lon=-79.30, device_probability=0.05,
        )
        await insert_observation(
            conn, "ob1", device_id="dv1", ts="2026-05-27T10:29:59.8+00:00",
            lat=43.30, lon=-79.30, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0,
        )

    # First pass: no server score yet, so the pair is scored off device_probability.
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        before = await conn.fetchval(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='fr1'"
        )

    # Detection lands later and disagrees sharply with the phone.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE asset_frame SET server_probability=0.95, server_model_id='m', "
            "detected_at=now() WHERE client_id='fr1'"
        )

    # Without the nudge, fusion does not reconsider a processed frame.
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        unchanged = await conn.fetchval(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='fr1'"
        )
    assert unchanged == before, "a processed frame must not be re-paired by itself"

    # The backfill's reset, verbatim.
    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE asset_frame SET processed_at = NULL "
            "WHERE detected_at IS NOT NULL AND server_probability IS NOT NULL"
        )
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT fused_confidence FROM fusion_pair WHERE frame_client_id='fr1'"
        )
    assert len(rows) == 1, "re-fusing must upsert in place, not add a second pair"
    assert rows[0]["fused_confidence"] > before, (
        "the stronger server probability should raise the fused confidence"
    )


async def test_scoring_persists_the_full_class_posterior(db_pool):
    """Phase 2.2c: the vector, not just its pothole component.

    _class_posteriors always computed the whole {pothole, crack, not} posterior and
    score_observation used to discard everything but the argmax and P(pothole). The
    spatiotemporal integration combines distributions across cluster members, so the
    vector has to survive to the database. Pinned here because nothing else would
    notice it silently going back to a scalar.
    """
    import json

    async with db_pool.acquire() as conn:
        await _seed_three_blobs(conn, n_per_blob=80, prefix="post")  # 240 >= gate
    assert await run_fit_job(db_pool) is not None
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT sensor_p_pothole, sensor_class, sensor_class_probs "
            "FROM asset_observation WHERE scored_at IS NOT NULL"
        )

    assert rows, "the fusion job should have scored the seeded observations"
    with_probs = [r for r in rows if r["sensor_class_probs"] is not None]
    assert with_probs, "no observation carried a class posterior"

    for r in with_probs:
        probs = json.loads(r["sensor_class_probs"])
        assert sum(probs.values()) == pytest.approx(1.0), probs
        # The scalar must stay consistent with the vector it came from, or the read
        # path and the integration would disagree about the same observation.
        assert probs.get("pothole", 0.0) == pytest.approx(r["sensor_p_pothole"])
        # And the stored class must be the vector's argmax.
        assert max(probs, key=probs.get) == r["sensor_class"]


# ── Pairing search (Phase 2.2d) ───────────────────────────────────────────────
#
# The camera resolves a pothole while it is still AHEAD of the vehicle, so the
# frame that actually saw it was taken EARLIER and some metres SHORT. The pre-2.2d
# ranking minimised |delta_t| then distance, i.e. it preferred the frame taken on
# top of the pothole. Re-ranking pothole_db's real candidates under the cost model
# changed the winner for 713 of 2197 frames (32.5%).
#
# Distances below are built by offsetting latitude, which is ~111320 m per degree
# everywhere; using latitude rather than longitude keeps the conversion independent
# of the test's latitude.
_M_PER_DEG_LAT = 111_320.0
_LAT0 = 43.6532
_LON0 = -79.3832


def _lat_north_of_origin(metres: float) -> float:
    """Latitude `metres` north of the fixture origin."""
    return _LAT0 + metres / _M_PER_DEG_LAT


requires_pairing_cost = pytest.mark.skipif(
    not settings.fusion_pairing_cost_enabled,
    reason="FUSION_PAIRING_COST_ENABLED is off; the pre-2.2d ranking is in use",
)


async def test_pairing_rejects_a_candidate_outside_the_spatial_window(db_pool):
    """ST_DWithin was deletable without failing the suite before this test.

    Every pre-2.2d fusion test placed the frame and the observation at identical
    coordinates, so nothing exercised the spatial gate at all.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-spatial", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        # 200 m away: same instant, far outside fusion_window_m (40 m).
        await insert_observation(
            conn, "o-far-away", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00",
            lat=_lat_north_of_origin(200.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert pairs == []


@requires_pairing_cost
async def test_pairing_records_the_ground_distance(db_pool):
    """delta_m was never asserted before; it could have been writing garbage."""
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-dm", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-dm", device_id="dev-1",
            # 1.15 s later: at 13 m/s the car covers the 15 m to the pothole.
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pair = await conn.fetchrow("SELECT * FROM fusion_pair")
    assert pair["delta_m"] == pytest.approx(15.0, abs=0.5)
    assert pair["delta_ms"] == -1150
    # A kinematically perfect candidate inside the lead band: cost ~= 0.
    assert pair["match_cost"] == pytest.approx(0.0, abs=0.05)


@requires_pairing_cost
async def test_pairing_prefers_the_frame_that_saw_the_pothole_ahead(db_pool):
    """The headline behaviour change. Fails under the pre-2.2d ranking.

    Two candidates for one frame:
      - `o-underfoot`: same place, same instant. The pothole is under the car, so
        the camera cannot have seen it. The old ranking picks this one, because
        |delta_t| = 0 is unbeatable.
      - `o-ahead`: 15 m on and 1.15 s later, i.e. exactly where a pothole would be
        when photographed at 13 m/s. This is the one that was actually imaged.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-lookahead", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-underfoot", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
        await insert_observation(
            conn, "o-ahead", device_id="dev-1",
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert len(pairs) == 1
    assert pairs[0]["event_client_id"] == "o-ahead"


@requires_pairing_cost
async def test_a_frame_taken_after_the_event_loses_to_one_taken_before(db_pool):
    """A frame shot after the impact is looking at road already crossed.

    Both candidates are the same distance out and the same time offset in
    magnitude, so only the sign differs. Admissible either way -- GPS and clock
    noise straddle zero -- but the backward one must win.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-sign", device_id="dev-1",
                           ts="2026-05-27T10:30:10+00:00", lat=_LAT0, lon=_LON0)
        # Event 1.15 s AFTER the frame: the car is driving toward it. Preferred.
        await insert_observation(
            conn, "o-before", device_id="dev-1",
            ts="2026-05-27T10:30:11.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
        # Event 1.15 s BEFORE the frame: already behind the car.
        await insert_observation(
            conn, "o-after", device_id="dev-1",
            ts="2026-05-27T10:30:08.85+00:00",
            lat=_lat_north_of_origin(-15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert len(pairs) == 1
    assert pairs[0]["event_client_id"] == "o-before"


@requires_pairing_cost
async def test_the_temporal_window_tightens_as_speed_rises(db_pool):
    """The two gates are one constraint (window_m / speed), not two.

    A fixed 3000 ms window meant 39 m of travel at the measured median 13.02 m/s
    and 75 m at p90 -- both wider than the spatial gate, so one of the two was
    always slack. The same 2.5 s candidate is now admissible at 5 m/s and rejected
    at 25 m/s.
    """
    async with db_pool.acquire() as conn:
        # Slow: derived window = min(8000, 1000*40/5) = 8000 ms. 2500 ms is inside.
        await insert_frame(conn, "f-slow", device_id="dev-slow",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-slow", device_id="dev-slow",
            ts="2026-05-27T10:30:02.5+00:00",
            lat=_lat_north_of_origin(10.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=5.0,
        )
        # Fast: derived window = min(8000, 1000*40/25) = 1600 ms. 2500 ms is outside.
        await insert_frame(conn, "f-fast", device_id="dev-fast",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-fast", device_id="dev-fast",
            ts="2026-05-27T10:30:02.5+00:00",
            lat=_lat_north_of_origin(10.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=25.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        paired = {r["event_client_id"] for r in await conn.fetch("SELECT * FROM fusion_pair")}
    assert paired == {"o-slow"}


async def test_pairing_prefers_the_frames_own_device_when_costs_tie(db_pool):
    """Only the negative case (no partner at all) was tested before.

    Two devices' observations are byte-identical in geometry, so the cost cannot
    separate them; the device_id join must. Note dev-2's id sorts AFTER dev-1's
    observation id here, so a broken join would be caught by the assertion rather
    than accidentally satisfying it.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-dev", device_id="dev-2",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "aaa-obs-other-device", device_id="dev-1",
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
        await insert_observation(
            conn, "zzz-obs-own-device", device_id="dev-2",
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert len(pairs) == 1
    assert pairs[0]["event_client_id"] == "zzz-obs-own-device"


async def test_a_cost_tie_resolves_deterministically(db_pool):
    """Byte-identical reruns are a property the repo relies on.

    Mirroring the candidates north and south of the frame does NOT tie: ST_Distance
    on geography is spheroidal, so 15 m north and 15 m south of one point differ in
    the sixth decimal and the kinematic term separates them. The genuine tie is two
    observations sharing one GPS fix and one timestamp -- which is not contrived:
    119 observations in pothole_db share an identical (device_id, geom, ts_utc),
    because the app reuses a fix across consecutive readings.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-tie", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        for cid in ("obs-a-tie", "obs-b-tie"):
            await insert_observation(
                conn, cid, device_id="dev-1",
                ts="2026-05-27T10:30:01.15+00:00",
                lat=_lat_north_of_origin(15.0), lon=_LON0,
                magnitude=6.0, accel_std=1.0, speed_mps=13.0,
            )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        first = [r["event_client_id"] for r in await conn.fetch("SELECT * FROM fusion_pair")]
        # Re-fuse the same frame and confirm the choice does not drift.
        await conn.execute("UPDATE asset_frame SET processed_at = NULL")
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        second = [r["event_client_id"] for r in await conn.fetch("SELECT * FROM fusion_pair")]
    assert first == ["obs-a-tie"]  # lexicographically smaller id wins
    assert first == second


async def test_exactly_one_frame_is_primary_per_observation(db_pool):
    """is_primary is per-observation but pairing runs per frame batch.

    Two frames image the same pothole from different distances, in two separate
    fusion runs. The later run finds the better view, so the earlier primary has to
    be demoted -- and because idx_fusion_pair_primary is a partial UNIQUE index,
    getting that order wrong aborts the transaction rather than corrupting data.
    """
    async with db_pool.acquire() as conn:
        # Run 1: a poor view -- 38 m out, beyond the lead band's far edge.
        await insert_frame(conn, "f-poor", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-shared", device_id="dev-1",
            ts="2026-05-27T10:30:02.9+00:00",
            lat=_lat_north_of_origin(38.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        # Run 2: a good view -- 15 m out, squarely inside the band.
        await insert_frame(
            conn, "f-good", device_id="dev-1",
            ts="2026-05-27T10:30:01.75+00:00",
            lat=_lat_north_of_origin(23.0), lon=_LON0,
        )
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT frame_client_id, is_primary, match_cost FROM fusion_pair "
            "WHERE event_client_id = 'o-shared' ORDER BY frame_client_id"
        )
    assert len(rows) == 2, "both views should be kept as an audit trail"
    primaries = [r["frame_client_id"] for r in rows if r["is_primary"]]
    assert primaries == ["f-good"]


async def test_the_legacy_ranking_is_restored_by_the_kill_switch(db_pool, monkeypatch):
    """FUSION_PAIRING_COST_ENABLED=false must reproduce the pre-2.2d choice."""
    from app.config import settings

    monkeypatch.setattr(settings, "fusion_pairing_cost_enabled", False)
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-legacy", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        await insert_observation(
            conn, "o-underfoot", device_id="dev-1",
            ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
        await insert_observation(
            conn, "o-ahead", device_id="dev-1",
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)
    async with db_pool.acquire() as conn:
        pairs = await conn.fetch("SELECT * FROM fusion_pair")
    assert len(pairs) == 1
    # The old ranking's unbeatable |delta_t| = 0 -- the exact inversion 2.2d fixes.
    assert pairs[0]["event_client_id"] == "o-underfoot"
    assert pairs[0]["match_cost"] is None


async def test_refusing_corrects_a_frames_pair_instead_of_appending(db_pool):
    """The activation path for Phase 2.2d must not leave a contradictory row.

    fusion_pair is keyed (event_client_id, frame_client_id), so an upsert can only
    overwrite a pair whose BOTH ends are unchanged. Re-fusing under the new ranking
    reassigns ~37% of frames to a different observation, and without an explicit
    delete each of those would leave its old row behind under the old event id --
    two populations in one table, with nothing marking which is current.
    """
    async with db_pool.acquire() as conn:
        await insert_frame(conn, "f-refuse", device_id="dev-1",
                           ts="2026-05-27T10:30:00+00:00", lat=_LAT0, lon=_LON0)
        # 38 m out: kinematically consistent, but past the lead band's far edge,
        # so it carries an 8 m penalty. Separating the two candidates by the BAND
        # rather than by a fraction of a second matters -- ST_Distance is spheroidal,
        # so a latitude offset of "15 m" is 14.97 m here, and a test whose margin is
        # milliseconds of kinematic residual would turn on that rounding.
        await insert_observation(
            conn, "o-first-choice", device_id="dev-1",
            ts="2026-05-27T10:30:02.92+00:00",
            lat=_lat_north_of_origin(38.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        before = await conn.fetch("SELECT * FROM fusion_pair")
        assert [r["event_client_id"] for r in before] == ["o-first-choice"]
        # A clearly better candidate arrives late, and the frame is re-queued.
        await insert_observation(
            conn, "o-better-choice", device_id="dev-1",
            ts="2026-05-27T10:30:01.15+00:00",
            lat=_lat_north_of_origin(15.0), lon=_LON0,
            magnitude=6.0, accel_std=1.0, speed_mps=13.0,
        )
        await conn.execute("UPDATE asset_frame SET processed_at = NULL")
    await run_fusion_job(db_pool)

    async with db_pool.acquire() as conn:
        after = await conn.fetch("SELECT * FROM fusion_pair")

    assert len(after) == 1, "one frame holds one pair, not one per ranking it saw"
    assert after[0]["event_client_id"] == "o-better-choice"
    # And the displaced observation keeps no orphaned primary flag.
    async with db_pool.acquire() as conn:
        orphan = await conn.fetchval(
            "SELECT count(*) FROM fusion_pair WHERE event_client_id = 'o-first-choice'"
        )
    assert orphan == 0
