"""Tests for the operator dashboard's cluster detail panel (Phase 2.5).

The frame join is the fragile part: the clustering job only writes
kind='observation' links and asset_frame.event_client_id is a nullable hint that
is almost always NULL, so frames have to be reached through fusion_pair. Several
tests below exist specifically to fail if someone "simplifies" that.
"""

import json
from pathlib import Path

import pytest

from app.config import settings
from app.services.cluster_detail_service import MAX_FRAMES, MAX_MEMBERS, _device_label
from tests import MINIMAL_JPEG
from tests.conftest import insert_frame, insert_observation
from tests.test_tiles import auth

LAT, LON = 43.6532, -79.3832


async def _insert_cluster(conn, cluster_id="clu_a", *, repaired=False,
                          distinct_passes=3, member_span_s=259200.0):
    # distinct_passes and member_span_s default to 0 / NULL in the schema, so a
    # fixture that left them out would pass against a service that never selects
    # them -- `0 == 0 ?? 0`. That is exactly how the panel came to print
    # "0 passes" about every cluster while the table said 1. Write real values.
    await conn.execute(
        """
        INSERT INTO asset_cluster (
            cluster_id, asset_type, centroid, severity, confidence,
            observation_count, distinct_devices, distinct_passes, member_span_s,
            last_seen, source, repaired_at
        ) VALUES (
            $1, 'pothole', ST_SetSRID(ST_MakePoint($2, $3), 4326)::geography,
            2.5, 0.9, 3, 2, $5, $6, now(), 'crowd',
            CASE WHEN $4 THEN now() ELSE NULL END
        )
        """,
        cluster_id, LON, LAT, repaired, distinct_passes, member_span_s,
    )


async def _link(conn, cluster_id, member_id, confidence=0.7):
    await conn.execute(
        "INSERT INTO observation_cluster_link (cluster_id, member_id, kind, fused_confidence) "
        "VALUES ($1, $2, 'observation', $3)",
        cluster_id, member_id, confidence,
    )


async def _pair(conn, event_id, frame_id, *, fused=0.8, delta_ms=250, delta_m=3.0):
    await conn.execute(
        "INSERT INTO fusion_pair (event_client_id, frame_client_id, fused_confidence, "
        "delta_ms, delta_m) VALUES ($1, $2, $3, $4, $5)",
        event_id, frame_id, fused, delta_ms, delta_m,
    )


def _write_jpeg(jpeg_url: str) -> Path:
    """Materialise a real file for a frame row's jpeg_url."""
    path = (Path(settings.storage_local_path).resolve() / jpeg_url)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(MINIMAL_JPEG)
    return path


# ── device_ref labelling (no DB) ──────────────────────────────────────────────

class TestDeviceLabel:
    @pytest.mark.parametrize(("rank", "label"), [(1, "A"), (2, "B"), (26, "Z"), (27, "AA")])
    def test_ordinals_render_as_letters(self, rank, label):
        assert _device_label(rank) == label


# ── Detail payload ────────────────────────────────────────────────────────────

class TestClusterDetail:
    async def test_unknown_cluster_is_404(self, client, db_pool):
        resp = await client.get("/api/v1/clusters/clu_nope", headers=auth())
        assert resp.status_code == 404

    async def test_unauthenticated_is_401(self, client, db_pool):
        resp = await client.get("/api/v1/clusters/clu_a")
        assert resp.status_code == 401

    async def test_members_returned_with_sensor_detail(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            for i in range(3):
                await insert_observation(conn, f"obs-{i}", device_id=f"dev-{i}")
                await _link(conn, "clu_a", f"obs-{i}")

        resp = await client.get("/api/v1/clusters/clu_a", headers=auth())
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["members"]) == 3
        assert body["members_truncated"] is False
        assert {m["device_ref"] for m in body["members"]} == {"A", "B", "C"}
        assert all("client_id" in m and "fused_confidence" in m for m in body["members"])

    async def test_corroboration_fields_reach_the_client(self, client, db_pool):
        """distinct_passes and member_span_s must survive the whole path.

        They were declared on the client and consumed by the panel while
        _HEADER_SQL never selected them and ClusterDetailResponse never defined
        them, so the console asserted "0 passes" about every cluster. The fixture
        writes 3 and 259200.0 precisely so a regression to the default cannot pass.
        """
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)

        resp = await client.get("/api/v1/clusters/clu_a", headers=auth())
        assert resp.status_code == 200
        body = resp.json()
        assert body["distinct_passes"] == 3
        assert body["member_span_s"] == pytest.approx(259200.0)

    async def test_single_pass_cluster_reports_its_span(self, client, db_pool):
        """The single-drive-past case, which is what the real corpus looks like."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, distinct_passes=1, member_span_s=12.0)

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert body["distinct_passes"] == 1
        assert body["member_span_s"] == pytest.approx(12.0)

    async def test_same_device_shares_one_device_ref(self, client, db_pool):
        """The corroboration signal: 3 hits from one device is not 3 devices."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            for i in range(3):
                await insert_observation(conn, f"obs-{i}", device_id="dev-same")
                await _link(conn, "clu_a", f"obs-{i}")

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert {m["device_ref"] for m in body["members"]} == {"A"}

    async def test_frames_reached_through_fusion_pair(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            await insert_observation(conn, "obs-0")
            await _link(conn, "clu_a", "obs-0")
            await insert_frame(conn, "frm-0", jpeg_url="dev-1/frm-0.jpg")
            await _pair(conn, "obs-0", "frm-0")

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert len(body["frames"]) == 1
        frame = body["frames"][0]
        assert frame["client_id"] == "frm-0"
        assert frame["paired_observation_id"] == "obs-0"
        assert frame["delta_ms"] == 250
        assert frame["image_url"] == "/api/v1/frames/frm-0/image"

    async def test_frame_linked_only_by_event_client_id_is_not_found(self, client, db_pool):
        """asset_frame.event_client_id is a client hint, not the linkage.

        Fusion never reads it, so a frame with only that link must NOT appear —
        this pins the documented join and fails if someone adds an OR on it.
        """
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            await insert_observation(conn, "obs-0")
            await _link(conn, "clu_a", "obs-0")
            await insert_frame(conn, "frm-hint")
            await conn.execute(
                "UPDATE asset_frame SET event_client_id = 'obs-0' WHERE client_id = 'frm-hint'"
            )

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert body["frames"] == []

    async def test_one_observation_with_two_frames_does_not_duplicate_the_member(
        self, client, db_pool
    ):
        """A joined query would multiply member rows by frame count."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            await insert_observation(conn, "obs-0")
            await _link(conn, "clu_a", "obs-0")
            for i in range(2):
                await insert_frame(conn, f"frm-{i}", jpeg_url=f"dev-1/frm-{i}.jpg")
                await _pair(conn, "obs-0", f"frm-{i}", fused=0.5 + i * 0.1)

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert len(body["members"]) == 1
        assert len(body["frames"]) == 2

    async def test_frame_paired_to_two_observations_appears_once(self, client, db_pool):
        """DISTINCT ON collapses the other direction of the fan-out."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            for i in range(2):
                await insert_observation(conn, f"obs-{i}")
                await _link(conn, "clu_a", f"obs-{i}")
            await insert_frame(conn, "frm-0")
            await _pair(conn, "obs-0", "frm-0", fused=0.4)
            await _pair(conn, "obs-1", "frm-0", fused=0.9)

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert len(body["frames"]) == 1
        # It carries its strongest pairing, not an arbitrary one.
        assert body["frames"][0]["fused_confidence"] == pytest.approx(0.9)

    async def test_frame_limit_is_enforced_and_flagged(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            await insert_observation(conn, "obs-0")
            await _link(conn, "clu_a", "obs-0")
            for i in range(4):
                await insert_frame(conn, f"frm-{i}", jpeg_url=f"dev-1/frm-{i}.jpg")
                await _pair(conn, "obs-0", f"frm-{i}")

        body = (
            await client.get("/api/v1/clusters/clu_a?frame_limit=2", headers=auth())
        ).json()
        assert len(body["frames"]) == 2
        assert body["frames_truncated"] is True

    async def test_frame_limit_above_the_cap_is_rejected(self, client, db_pool):
        resp = await client.get(
            f"/api/v1/clusters/clu_a?frame_limit={MAX_FRAMES + 1}", headers=auth()
        )
        assert resp.status_code == 422

    async def test_repaired_cluster_is_still_readable(self, client, db_pool):
        """The tile layer hides repaired clusters; the detail panel must not."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn, repaired=True)

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert body["repaired_at"] is not None


class TestAnonymity:
    async def test_response_leaks_neither_device_id_nor_storage_path(self, client, db_pool):
        """roadmap §2.11: device_id is never exposed. jpeg_url embeds it."""
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            await insert_observation(conn, "obs-0", device_id="super-secret-device")
            await _link(conn, "clu_a", "obs-0")
            await insert_frame(
                conn, "frm-0", device_id="super-secret-device",
                jpeg_url="super-secret-device/frm-0.jpg",
            )
            await _pair(conn, "obs-0", "frm-0")

        raw = (await client.get("/api/v1/clusters/clu_a", headers=auth())).text
        assert "super-secret-device" not in raw
        assert "jpeg_url" not in raw


class TestMemberCap:
    async def test_members_truncated_flag(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await _insert_cluster(conn)
            for i in range(MAX_MEMBERS + 5):
                await insert_observation(conn, f"obs-{i:04d}", device_id=f"dev-{i % 3}")
                await _link(conn, "clu_a", f"obs-{i:04d}")

        body = (await client.get("/api/v1/clusters/clu_a", headers=auth())).json()
        assert len(body["members"]) == MAX_MEMBERS
        assert body["members_truncated"] is True
        # dense_rank runs before LIMIT, so labels stay consistent with the full set.
        assert {m["device_ref"] for m in body["members"]} <= {"A", "B", "C"}


# ── Frame images ──────────────────────────────────────────────────────────────

class TestFrameImage:
    async def test_serves_the_jpeg(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-0", jpeg_url="dev-1/frm-0.jpg")
        path = _write_jpeg("dev-1/frm-0.jpg")

        resp = await client.get("/api/v1/frames/frm-0/image", headers=auth())
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"
        assert resp.content == MINIMAL_JPEG
        assert resp.headers["cache-control"].startswith("private")
        path.unlink()

    async def test_unknown_frame_is_404(self, client, db_pool):
        resp = await client.get("/api/v1/frames/nope/image", headers=auth())
        assert resp.status_code == 404

    async def test_row_without_a_file_is_404_not_500(self, client, db_pool):
        """Starlette's FileResponse raises RuntimeError on a missing file."""
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-missing", jpeg_url="dev-1/does-not-exist.jpg")

        resp = await client.get("/api/v1/frames/frm-missing/image", headers=auth())
        assert resp.status_code == 404

    async def test_jpeg_url_escaping_the_storage_root_is_refused(self, client, db_pool):
        """jpeg_url is unconstrained TEXT; a bad row must not become a file read."""
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-evil", jpeg_url="../../../../../../etc/passwd")

        resp = await client.get("/api/v1/frames/frm-evil/image", headers=auth())
        assert resp.status_code == 404

    async def test_unauthenticated_is_401(self, client, db_pool):
        resp = await client.get("/api/v1/frames/frm-0/image")
        assert resp.status_code == 401

    async def test_thumb_size_is_rejected_until_implemented(self, client, db_pool):
        """Better a 422 than silently serving full-size to a client expecting a thumb."""
        resp = await client.get("/api/v1/frames/frm-0/image?size=thumb", headers=auth())
        assert resp.status_code == 422


class TestFrameDetail:
    """GET /api/v1/frames/{client_id} — one frame, paired or not.

    Exists because the frames TILE carries server_box_count but not the boxes, so
    opening a frame from the map needs a round trip.
    """

    async def test_unauthenticated_is_401(self, client, db_pool):
        assert (await client.get("/api/v1/frames/anything")).status_code == 401

    async def test_unknown_frame_is_404(self, client, db_pool):
        resp = await client.get("/api/v1/frames/no-such-frame", headers=auth())
        assert resp.status_code == 404

    async def test_an_unpaired_frame_is_served(self, client, db_pool):
        """THE CASE ClusterFrameItem COULD NOT EXPRESS.

        Its paired_observation_id is required, but the map's frames layer includes
        unpaired frames deliberately — a frame the detector scored that matched no
        sensor event reached no cluster, which is a fact about the pipeline worth
        being able to open. That is why this endpoint has its own model.
        """
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-lonely", device_probability=0.71)

        body = (await client.get("/api/v1/frames/frm-lonely", headers=auth())).json()
        assert body["client_id"] == "frm-lonely"
        assert body["paired_observation_id"] is None
        assert body["fused_confidence"] is None
        assert body["device_probability"] == pytest.approx(0.71)

    async def test_a_paired_frame_carries_its_pairing(self, client, db_pool):
        async with db_pool.acquire() as conn:
            await insert_observation(conn, "obs-p")
            await insert_frame(conn, "frm-paired")
            await _pair(conn, "obs-p", "frm-paired", fused=0.83, delta_ms=250, delta_m=3.0)

        body = (await client.get("/api/v1/frames/frm-paired", headers=auth())).json()
        assert body["paired_observation_id"] == "obs-p"
        assert body["fused_confidence"] == pytest.approx(0.83)
        assert body["delta_ms"] == 250

    async def test_it_leaks_neither_device_id_nor_the_storage_path(self, client, db_pool):
        """The stored path is "{device_id}/{client_id}.jpg", so one field would leak
        the device. Asserted on the serialized body rather than trusted from the query."""
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-priv", device_id="dev-secret",
                               jpeg_url="dev-secret/frm-priv.jpg")

        resp = await client.get("/api/v1/frames/frm-priv", headers=auth())
        raw = resp.text
        assert "dev-secret" not in raw
        assert "jpeg_url" not in resp.json()
        # ...and the authenticated path is what is offered instead.
        assert resp.json()["image_url"] == "/api/v1/frames/frm-priv/image"

    async def test_boxes_are_parsed_and_the_vlm_verdict_is_lifted_out(self, client, db_pool):
        """server_detections carries the hybrid backend's {"_vlm_verdict": ...} element,
        which has no bbox and must not reach a box renderer."""
        detections = json.dumps([
            {"bbox": {"x": 0.1, "y": 0.2, "w": 0.3, "h": 0.4}, "confidence": 0.7,
             "label": "pothole", "class_id": 0},
            {"_vlm_verdict": {"is_pothole": True, "confidence": 0.9, "severity": "deep",
                              "rationale": "a bowl-shaped cavity", "model_id": "qwen"}},
        ])
        async with db_pool.acquire() as conn:
            await insert_frame(conn, "frm-boxes")
            await conn.execute(
                "UPDATE asset_frame SET server_detections = $2::jsonb, detected_at = now() "
                "WHERE client_id = $1", "frm-boxes", detections,
            )

        body = (await client.get("/api/v1/frames/frm-boxes", headers=auth())).json()
        assert len(body["server_boxes"]) == 1            # the verdict is NOT a box
        assert body["server_boxes"][0]["confidence"] == pytest.approx(0.7)
        assert body["vlm_verdict"]["is_pothole"] is True
        assert body["detected_at"] is not None
