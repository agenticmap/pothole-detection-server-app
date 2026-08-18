"""Seed deterministic synthetic clusters for dashboard demos (Phase 2.5b).

The operator dashboard is unreviewable over an empty extent: an empty map and a
broken map look identical. This fills a TEST database with enough shaped data to
exercise every visual path — all four severity tiers, unrated clusters, repaired
clusters, multi-entry repair timelines, and camera frames the detail panel can
actually render.

Usage (from the repo root — storage paths resolve against the CWD):

    DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_test \
        python scripts/seed_demo.py

    ... --reset          remove a previous seed first
    ... --clusters 250   more of them

THE DATA IS NOT REAL AND NOT CALIBRATED. Severity is drawn uniformly so that all
four tiers are visible; a real drive skews heavily toward the low tier. Nothing
here should be used to judge model quality or to size a repair budget.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import shutil
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

# Run directly (`python scripts/seed_demo.py`) without installing the package.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config import settings  # noqa: E402
from app.database import create_pool, run_migrations  # noqa: E402

# This script writes thousands of fabricated rows. Guard on the database name
# rather than trusting whoever set DATABASE_URL — the same reasoning, and the
# same allow-list, as tests/conftest.py. The dev database holds real collected
# drive data; synthetic clusters there would also skew the sensor-model fit.
_ALLOWED_SEED_DATABASES = frozenset({"pothole_test", "pothole_ci"})

# Every row this script writes is tagged so --reset can remove exactly its own
# work instead of TRUNCATEing (which would destroy a parallel test run).
CLUSTER_PREFIX = "clu_demo"
DEVICE_PREFIX = "demo-dev-"
OBS_PREFIX = "demo-obs-"
FRAME_PREFIX = "demo-frm-"

# Toronto, matching the design mockup's own generator so the seeded extent and
# the mockup show the same shape of city.
CENTRE_LAT, CENTRE_LON = 43.6532, -79.3832
SPREAD_LAT, SPREAD_LON = 0.0425, 0.08

# Severity is clamped to [0, 1] by app/sensor_model/features.py, and the
# dashboard's tier floors (dashboard/src/severity.ts) are 0 / 0.25 / 0.5 / 0.75.
SEVERITY_MAX = 0.98

REPAIR_NOTES = [
    "Cold patch applied, crew 4.",
    "Full-depth repair; section milled and repaved.",
    "Temporary fill — scheduled for permanent repair next cycle.",
    "Verified on site, no defect found at this location.",
    "Repaired under the spring resurfacing contract.",
    None,
]
REOPEN_NOTES = [
    "Patch failed after freeze-thaw; reopening.",
    "Resident report — defect present again.",
    "Closed in error.",
]


def _database_name(dsn: str) -> str:
    return urlsplit(dsn).path.lstrip("/")


def _metres_to_deg(metres: float, lat: float) -> tuple[float, float]:
    """Rough local metres -> (dlat, dlon). Good enough for a 25 m jitter."""
    dlat = metres / 111_320.0
    dlon = metres / (111_320.0 * max(math.cos(math.radians(lat)), 1e-6))
    return dlat, dlon


def _write_jpeg(path: Path, rng: random.Random, severity: float | None) -> None:
    """Write a plausible-looking road frame.

    Deliberately synthetic and obviously so: a grainy road-grey field with a
    darker defect blob that grows with severity.
    """
    import numpy as np
    from PIL import Image, ImageDraw

    width, height = 320, 240
    base = 96 + rng.randint(-12, 12)

    # Per-pixel grain, generated with numpy rather than 1400 draw.point calls.
    # It is not decoration: smooth synthetic images compress to 2 KB, which would
    # make the panel gallery's concurrency cap untestable. Real frames measure
    # 12-62 KB and grain is most of that.
    noise = np.random.default_rng(rng.getrandbits(32)).normal(0, 17, (height, width, 1))
    field = np.clip(np.array([base, base - 4, base - 10]) + noise, 0, 255).astype("uint8")
    image = Image.fromarray(field, mode="RGB")
    draw = ImageDraw.Draw(image)

    # Lane edge.
    lane_y = rng.randint(30, 70)
    draw.line([(0, lane_y), (width, lane_y + rng.randint(-8, 8))], fill=(196, 190, 176), width=3)

    # The defect: darker and larger as severity rises.
    scale = 0.35 if severity is None else 0.35 + severity
    radius_x = int(28 * scale)
    radius_y = int(18 * scale)
    cx = rng.randint(width // 3, 2 * width // 3)
    cy = rng.randint(height // 2, height - radius_y - 12)
    draw.ellipse(
        [cx - radius_x, cy - radius_y, cx + radius_x, cy + radius_y],
        fill=(38, 33, 30),
        outline=(64, 57, 50),
        width=3,
    )

    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="JPEG", quality=86)


_INSERT_CLUSTER = """
    INSERT INTO asset_cluster (
        cluster_id, asset_type, centroid, severity, confidence,
        observation_count, distinct_devices, last_seen, source,
        repaired_at, created_at, updated_at
    ) VALUES (
        $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
        $4, $5, $6, $7, $8, 'crowd', $9, $10, $10
    )
"""

_INSERT_OBSERVATION = """
    INSERT INTO asset_observation (
        client_id, device_id, asset_type, ts_utc, geom, speed_mps, bearing_deg,
        accel_max_g, accel_std, magnitude, gbar_in_max, confidence, received_at,
        sensor_class, sensor_p_pothole, sensor_severity, sensor_is_outlier,
        sensor_model_version, scored_at
    ) VALUES (
        $1, $2, 'pothole', $3, ST_SetSRID(ST_MakePoint($4, $5), 4326)::geography,
        $6, $7, $8, $9, $10, $11, $12, $13,
        'pothole', $14, $15, FALSE, 'demo-seed', $13
    )
"""

# kind='observation' is not a detail: both _MEMBERS_SQL and _FRAMES_SQL in
# app/services/cluster_detail_service.py filter on it, so a 'frame' row would be
# silently invisible to the panel.
_INSERT_LINK = """
    INSERT INTO observation_cluster_link (cluster_id, member_id, kind, fused_confidence)
    VALUES ($1, $2, 'observation', $3)
"""

_INSERT_FRAME = """
    INSERT INTO asset_frame (
        client_id, device_id, event_client_id, ts_utc, geom,
        device_probability, device_model_id, jpeg_url,
        server_probability, server_model_id, received_at, processed_at, detected_at
    ) VALUES (
        $1, $2, $3, $4, ST_SetSRID(ST_MakePoint($5, $6), 4326)::geography,
        $7, 'demo-device-v1', $8, $9, 'demo-server-v1', $10, $10, $10
    )
"""

# The panel reaches frames through fusion_pair, NOT through
# asset_frame.event_client_id — tests/test_cluster_detail.py exists specifically
# to prove a frame linked only by that column never appears.
_INSERT_PAIR = """
    INSERT INTO fusion_pair (
        event_client_id, frame_client_id, fused_confidence,
        delta_ms, delta_m, severity, created_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7)
"""

_INSERT_REPAIR = """
    INSERT INTO repair_log (
        repair_id, cluster_id, action, note, user_id, org_id, repaired_at, created_at
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
"""


async def _reset(conn, storage_root: Path) -> None:
    """Remove a previous seed, and only a previous seed."""
    # repair_log's FK to asset_cluster is NO ACTION (audit rows outlive the
    # cluster), so it has to go first or the cluster delete raises.
    steps = [
        ("repair_log", "DELETE FROM repair_log WHERE cluster_id LIKE $1", CLUSTER_PREFIX + "%"),
        # observation_cluster_link cascades from asset_cluster.
        (
            "asset_cluster",
            "DELETE FROM asset_cluster WHERE cluster_id LIKE $1",
            CLUSTER_PREFIX + "%",
        ),
        # fusion_pair cascades from both of these.
        ("asset_frame", "DELETE FROM asset_frame WHERE device_id LIKE $1", DEVICE_PREFIX + "%"),
        (
            "asset_observation",
            "DELETE FROM asset_observation WHERE device_id LIKE $1",
            DEVICE_PREFIX + "%",
        ),
    ]
    for table, sql, pattern in steps:
        print(f"  reset {table}: {await conn.execute(sql, pattern)}")

    for path in sorted(storage_root.glob(DEVICE_PREFIX + "*")):
        if path.is_dir():
            shutil.rmtree(path)
            print(f"  reset frames: {path}")


async def seed(*, cluster_count: int, seed_value: int, do_reset: bool) -> None:
    db_name = _database_name(settings.database_url)
    if db_name not in _ALLOWED_SEED_DATABASES:
        raise SystemExit(
            f"Refusing to seed synthetic data into database {db_name!r}.\n"
            f"This script writes thousands of fabricated rows. Point DATABASE_URL at one of "
            f"{sorted(_ALLOWED_SEED_DATABASES)}, e.g.\n"
            f"  DATABASE_URL=postgresql://pothole:pothole@localhost:5433/pothole_test "
            f"python scripts/seed_demo.py"
        )

    storage_root = Path(settings.storage_local_path).resolve()
    rng = random.Random(seed_value)
    now = datetime.now(UTC)

    clusters: list[tuple] = []
    observations: list[tuple] = []
    links: list[tuple] = []
    frames: list[tuple] = []
    pairs: list[tuple] = []
    repairs: list[tuple] = []
    jpeg_jobs: list[tuple[Path, float | None]] = []

    pool = await create_pool()
    try:
        # Idempotent (a sorted glob of IF NOT EXISTS DDL), so a fresh
        # pothole_test works without a separate migration step.
        await run_migrations(pool)

        async with pool.acquire() as conn:
            operator = await conn.fetchrow(
                "SELECT s.user_id, s.email, m.org_id FROM staff_user s "
                "LEFT JOIN org_member m ON m.user_id = s.user_id "
                "ORDER BY s.created_at LIMIT 1"
            )
            if operator is None:
                print(
                    "  ! No staff_user exists, so repair history will show a raw user id\n"
                    "    instead of an email, and you will not be able to log in. Run:\n"
                    "      python scripts/create_staff.py --org org_demo --email you@example.com"
                )
            user_id = operator["user_id"] if operator else "usr_demo_seed"
            org_id = (operator["org_id"] if operator else None) or "org_demo"

            async with conn.transaction():
                if do_reset:
                    await _reset(conn, storage_root)

                for i in range(cluster_count):
                    cluster_id = f"{CLUSTER_PREFIX}{i:06d}"
                    lat = CENTRE_LAT + (rng.random() - 0.5) * 2 * SPREAD_LAT
                    lon = CENTRE_LON + (rng.random() - 0.5) * 2 * SPREAD_LON

                    # 6% unrated, so the "Unrated" legend row and the coalesce
                    # paths in layers.ts are genuinely exercised.
                    severity = (
                        None if rng.random() < 0.06 else round(rng.random() * SEVERITY_MAX, 2)
                    )
                    confidence = round(0.58 + rng.random() * 0.41, 2)

                    # last_seen is the trap: every tile query filters
                    # `last_seen >= now() - window`, and NULL >= x is NULL, so a
                    # cluster without it is invisible at every zoom, with no error.
                    last_seen = now - timedelta(
                        days=rng.uniform(0, settings.cluster_window_days - 5),
                        hours=rng.uniform(0, 23),
                    )

                    repaired = rng.random() < 0.22
                    repaired_at = (
                        last_seen + (now - last_seen) * rng.uniform(0.2, 0.95) if repaired else None
                    )

                    # Most clusters clear cluster_min_distinct_devices (2) so the
                    # public /api/v1/potholes read path is populated too; a few
                    # stay single-device for the operator-only triage queue.
                    device_count = 1 if rng.random() < 0.15 else rng.randint(2, 6)
                    devices = list(
                        dict.fromkeys(
                            f"{DEVICE_PREFIX}{rng.randrange(1, 40):02d}"
                            for _ in range(device_count)
                        )
                    )
                    member_count = rng.randint(max(3, len(devices)), 43)

                    clusters.append(
                        (
                            cluster_id,
                            lon,
                            lat,
                            severity,
                            confidence,
                            member_count,
                            len(devices),
                            last_seen,
                            repaired_at,
                            last_seen - timedelta(days=rng.uniform(1, 30)),
                        )
                    )

                    member_ids: list[str] = []
                    dlat, dlon = _metres_to_deg(settings.cluster_eps_m * 0.6, lat)
                    for j in range(member_count):
                        obs_id = f"{OBS_PREFIX}{i:06d}-{j:02d}"
                        member_ids.append(obs_id)
                        obs_ts = last_seen - timedelta(
                            days=rng.uniform(0, 4), minutes=rng.uniform(0, 1440)
                        )
                        # sensor_severity sits near the cluster's value by
                        # construction — the cluster figure IS the median of these.
                        base = 0.3 if severity is None else severity
                        obs_sev = min(1.0, max(0.0, base + rng.gauss(0, 0.06)))
                        speed = round(rng.uniform(6.0, 22.0), 2)
                        observations.append(
                            (
                                obs_id,
                                devices[j % len(devices)],
                                obs_ts,
                                lon + (rng.random() - 0.5) * 2 * dlon,
                                lat + (rng.random() - 0.5) * 2 * dlat,
                                speed,
                                round(rng.uniform(0, 359), 1),
                                round(1.4 + obs_sev * 3.0, 3),
                                round(0.5 + rng.random(), 3),
                                round(obs_sev * speed / settings.severity_scale, 3),
                                round(1.2 + rng.random(), 3),
                                confidence,
                                obs_ts,
                                round(min(0.99, confidence + rng.uniform(-0.05, 0.05)), 3),
                                round(obs_sev, 3),
                            )
                        )
                        links.append((cluster_id, obs_id, round(confidence, 3)))

                    # 18% of clusters are sensor-only, which is what makes the
                    # panel's "no frames" state worth having.
                    frame_count = 0 if rng.random() < 0.18 else rng.randint(2, 6)
                    for k in range(frame_count):
                        frame_id = f"{FRAME_PREFIX}{i:06d}-{k:02d}"
                        member_id = member_ids[rng.randrange(len(member_ids))]
                        device = devices[k % len(devices)]
                        frame_ts = last_seen - timedelta(hours=rng.uniform(0, 72))
                        jpeg_url = f"{device}/{frame_id}.jpg"
                        frames.append(
                            (
                                frame_id,
                                device,
                                member_id,
                                frame_ts,
                                lon + (rng.random() - 0.5) * 2 * dlon,
                                lat + (rng.random() - 0.5) * 2 * dlat,
                                round(rng.uniform(0.45, 0.97), 3),
                                jpeg_url,
                                round(rng.uniform(0.5, 0.98), 3),
                                frame_ts,
                            )
                        )
                        pairs.append(
                            (
                                member_id,
                                frame_id,
                                round(rng.uniform(0.5, 0.95), 3),
                                rng.randint(-900, 900),
                                round(rng.uniform(0.5, 12.0), 2),
                                severity,
                                frame_ts,
                            )
                        )
                        jpeg_jobs.append((storage_root / jpeg_url, severity))

                    if repaired_at is not None:
                        history: list[tuple[str, datetime]] = []
                        # A quarter get a failed patch, so the timeline has more
                        # than one entry to draw.
                        if rng.random() < 0.25:
                            first = last_seen + (repaired_at - last_seen) * 0.3
                            history.append(("repaired", first))
                            history.append(("unrepaired", first + (repaired_at - first) * 0.5))
                        history.append(("repaired", repaired_at))
                        for n, (action, when) in enumerate(history):
                            note = rng.choice(
                                REPAIR_NOTES if action == "repaired" else REOPEN_NOTES
                            )
                            repairs.append(
                                (
                                    f"rpr_demo{i:06d}{n:02d}",
                                    cluster_id,
                                    action,
                                    note,
                                    user_id,
                                    org_id,
                                    when if action == "repaired" else None,
                                    when,
                                )
                            )

                await conn.executemany(_INSERT_CLUSTER, clusters)
                await conn.executemany(_INSERT_OBSERVATION, observations)
                await conn.executemany(_INSERT_LINK, links)
                await conn.executemany(_INSERT_FRAME, frames)
                await conn.executemany(_INSERT_PAIR, pairs)
                await conn.executemany(_INSERT_REPAIR, repairs)
    finally:
        # Close the pool we made directly — database.close_pool() only closes the
        # module-global pool, which create_pool() never assigns.
        await pool.close()

    # Files last: a rolled-back transaction should not leave orphan JPEGs behind.
    # A frame row whose file is missing 404s per thumbnail rather than failing loudly.
    for path, severity in jpeg_jobs:
        _write_jpeg(path, rng, severity)

    print(
        f"Seeded {len(clusters)} clusters, {len(observations)} observations, "
        f"{len(frames)} frames ({len(jpeg_jobs)} JPEGs under {storage_root}), "
        f"and {len(repairs)} repair-log rows into {db_name!r}."
    )
    print(f"View it with:\n  DATABASE_URL={settings.database_url} uvicorn app.main:app --port 8010")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed synthetic clusters for dashboard demos.")
    parser.add_argument("--clusters", type=int, default=120, help="how many clusters to create")
    parser.add_argument("--seed", type=int, default=90210, help="RNG seed; same seed, same data")
    parser.add_argument("--reset", action="store_true", help="remove a previous seed first")
    args = parser.parse_args()

    if args.clusters < 1:
        sys.exit("--clusters must be at least 1.")

    asyncio.run(seed(cluster_count=args.clusters, seed_value=args.seed, do_reset=args.reset))


if __name__ == "__main__":
    main()
