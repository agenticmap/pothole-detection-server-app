"""The frame review API: the queue, the writes, and the rules that protect them.

The load-bearing test in this file is `TestQueueParity`. The queue predicate exists
twice — once in Python (`app/services/label_queue.wants_frame`, which the CLI uses and
`tests/test_label_queue.py` specifies) and once in SQL (`review_service._PREDICATES`,
because an HTTP endpoint cannot pull 5,615 rows into the process to filter them). Two
implementations of one rule drift. The parity test pins them together over a population
covering the whole truth table, so the SQL is a fast path *derived from* the Python
rather than a second opinion about it.

The rest guard properties that are cheap to break and expensive to notice:
  * A draft with ZERO boxes must be distinguishable from a frame nobody opened —
    only the first may ever be exported as a YOLO background image.
  * Box mode must never queue an unjudged frame.
  * Blind mode must not SEND the score, not merely hide it.
  * A revoked reviewer must stop being able to write ground truth immediately.
"""

import pytest

from app.services.label_queue import wants_frame
from app.services.review_service import BoxValidationError, validate_boxes
from tests.conftest import insert_frame
from tests.test_repair import _make_staff
from tests.test_tiles import auth

QUEUE = "/api/v1/review/frames"


@pytest.fixture(autouse=True)
async def _membership(db_pool):
    """The write tier is StaffOrAboveLive, which re-reads org_member rather than
    trusting the token's login-time snapshot. Without a membership row every write
    is a 403 -- which is the point of that tier, and is asserted directly in
    TestWriteAuthorization below."""
    async with db_pool.acquire() as conn:
        await _make_staff(conn, role="staff")


async def _frame(conn, client_id, *, score=None, device_p=0.5):
    await insert_frame(conn, client_id, device_probability=device_p,
                       jpeg_url=f"dev-1/{client_id}.jpg")
    if score is not None:
        await conn.execute(
            "UPDATE asset_frame SET server_probability = $2, detected_at = now() "
            "WHERE client_id = $1", client_id, score)


async def _label(conn, client_id, label, *, boxed=False, drafted=False, by="cli-sean"):
    await conn.execute(
        "INSERT INTO frame_label (frame_client_id, label, labeled_by) VALUES ($1, $2, $3) "
        "ON CONFLICT (frame_client_id) DO UPDATE SET label = EXCLUDED.label",
        client_id, label, by)
    if boxed:
        await conn.execute(
            "UPDATE frame_label SET boxed_at = now() WHERE frame_client_id = $1", client_id)
    if drafted:
        await conn.execute(
            "UPDATE frame_label SET boxes_drafted_at = now() WHERE frame_client_id = $1",
            client_id)


# ── Box validation (previously untested anywhere) ─────────────────────────────


class TestBoxValidation:
    """These rules lived only in the CLI's HTTP handler and had no tests at all.
    They are not belt-and-braces over migrations/013's CHECK constraints — they are
    what makes a bad box a 400 with a reason instead of a 500."""

    def ok(self, **kw):
        return {"class_id": 0, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2, **kw}

    def test_a_valid_box_survives(self):
        assert validate_boxes([self.ok()], 5) == [
            {"class_id": 0, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}
        ]

    def test_zero_boxes_is_valid(self):
        """Saving nothing is a real answer: 'reviewed, genuinely clean'."""
        assert validate_boxes([], 5) == []

    def test_an_unknown_class_is_refused(self):
        with pytest.raises(BoxValidationError, match="unknown class_id"):
            validate_boxes([self.ok(class_id=9)], 5)

    def test_a_zero_area_box_is_refused(self):
        with pytest.raises(BoxValidationError, match="no area"):
            validate_boxes([self.ok(w=0.0)], 5)

    def test_an_origin_outside_the_frame_is_refused(self):
        with pytest.raises(BoxValidationError, match="origin outside"):
            validate_boxes([self.ok(x=1.5)], 5)

    def test_a_box_running_past_the_edge_is_refused(self):
        with pytest.raises(BoxValidationError, match="past the frame"):
            validate_boxes([self.ok(x=0.9, w=0.5)], 5)

    def test_the_edge_slack_is_honoured(self):
        """migrations/013 allows 1.0001 to absorb the browser's pixel -> fraction
        rounding. A hard 1.0 would reject a box drawn flush to the frame edge, which
        is exactly where a pothole at the shoulder sits."""
        assert validate_boxes([self.ok(x=0.5, w=0.50005)], 5)

    def test_the_validator_agrees_with_the_check_constraint(self):
        """If the validator were looser than frame_box's CHECK, the API would 500
        where it should 400."""
        with pytest.raises(BoxValidationError):
            validate_boxes([self.ok(x=0.5, w=0.51)], 5)

    def test_malformed_input(self):
        with pytest.raises(BoxValidationError):
            validate_boxes("not a list", 5)
        with pytest.raises(BoxValidationError, match="malformed"):
            validate_boxes([{"class_id": 0, "x": "left"}], 5)


# ── Queue parity: the SQL must select exactly what wants_frame selects ─────────


class TestQueueParity:
    async def _population(self, conn):
        """Every reachable (label, boxed_at) state, so neither implementation can
        pass by handling only the common cases."""
        await _frame(conn, "f_unjudged", score=0.9)
        await _frame(conn, "f_judged", score=0.8)
        await _label(conn, "f_judged", 1)
        await _frame(conn, "f_boxed", score=0.7)
        await _label(conn, "f_boxed", 0, boxed=True)
        await _frame(conn, "f_unsure", score=0.6)
        await _label(conn, "f_unsure", -1)
        await _frame(conn, "f_unsure_boxed", score=0.5)
        await _label(conn, "f_unsure_boxed", -1, boxed=True)

        return await conn.fetch(
            "SELECT f.client_id, l.label, l.boxed_at FROM asset_frame f "
            "LEFT JOIN frame_label l ON l.frame_client_id = f.client_id")

    @pytest.mark.parametrize("mode", ["verdict", "box"])
    @pytest.mark.parametrize("review", [False, True])
    async def test_sql_and_python_select_the_same_frames(self, db_pool, client, mode, review):
        async with db_pool.acquire() as conn:
            rows = await self._population(conn)

        expected = {
            r["client_id"] for r in rows
            if wants_frame(r, box=(mode == "box"), review=review)
        }

        resp = await client.get(
            QUEUE, params={"mode": mode, "review": review, "limit": 200}, headers=auth())
        assert resp.status_code == 200
        got = {i["client_id"] for i in resp.json()["items"]}
        assert got == expected, f"SQL and wants_frame disagree for {mode}/{review}"

    async def test_box_mode_never_queues_an_unjudged_frame(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._population(conn)
        for review in (False, True):
            resp = await client.get(
                QUEUE, params={"mode": "box", "review": review}, headers=auth())
            assert "f_unjudged" not in {i["client_id"] for i in resp.json()["items"]}


# ── Ordering and bands ────────────────────────────────────────────────────────


class TestOrderingAndBands:
    async def _scored(self, conn):
        for i, s in enumerate([0.05, 0.35, 0.9, 0.5]):
            await _frame(conn, f"f{i}", score=s)
        await _frame(conn, "f_unscored", score=None)

    async def test_score_order_is_highest_first(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._scored(conn)
        resp = await client.get(QUEUE, params={"order": "score"}, headers=auth())
        got = [i["server_probability"] for i in resp.json()["items"]]
        assert got == sorted(got, reverse=True)

    async def test_unscored_frames_are_excluded_from_a_score_pass(self, db_pool, client):
        """A NULL score means the backfill has not seen the frame. Ranking it as 0.0
        would bury real frames under unscored ones. Matches rank_by_score."""
        async with db_pool.acquire() as conn:
            await self._scored(conn)
        resp = await client.get(QUEUE, params={"order": "score"}, headers=auth())
        assert "f_unscored" not in {i["client_id"] for i in resp.json()["items"]}

    async def test_the_band_filter_is_half_open(self, db_pool, client):
        """[min, max) so adjacent bands from the phase-2.9 table cannot double-count
        a frame sitting exactly on a boundary."""
        async with db_pool.acquire() as conn:
            await self._scored(conn)
        resp = await client.get(
            QUEUE, params={"min_score": 0.35, "max_score": 0.9}, headers=auth())
        got = {i["client_id"] for i in resp.json()["items"]}
        assert got == {"f1", "f3"}          # 0.35 included, 0.9 excluded

    async def test_an_inverted_band_is_refused(self, client):
        resp = await client.get(
            QUEUE, params={"min_score": 0.8, "max_score": 0.2}, headers=auth())
        assert resp.status_code == 400

    async def test_a_blind_pass_is_reproducible_from_its_seed(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._scored(conn)
        a = await client.get(QUEUE, params={"order": "blind", "seed": 42}, headers=auth())
        b = await client.get(QUEUE, params={"order": "blind", "seed": 42}, headers=auth())
        assert [i["client_id"] for i in a.json()["items"]] == \
               [i["client_id"] for i in b.json()["items"]]


class TestBlindModeWithholdsRatherThanHides:
    """The anti-anchoring rule has to be a server property. A score the browser has
    been given is one devtools panel away, and anchoring the labeller to the model is
    a MEASURED cause of bad labels (recall 0.708 -> 0.431 -> 0.354 across three
    successive labelling passes), not a style preference."""

    async def test_the_score_is_not_in_the_payload(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.77, device_p=0.66)
        item = (await client.get(
            QUEUE, params={"order": "blind"}, headers=auth())).json()["items"][0]
        assert item["server_probability"] is None
        assert item["device_probability"] is None

    async def test_model_boxes_cannot_be_opted_into_while_blind(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.77)
            await conn.execute(
                """UPDATE asset_frame SET server_detections =
                   '[{"bbox": {"x":0.1,"y":0.1,"w":0.2,"h":0.2}, "confidence":0.9}]'::jsonb
                   WHERE client_id = 'f_a'""")
        item = (await client.get(
            QUEUE,
            params={"order": "blind", "include_model_boxes": True},
            headers=auth())).json()["items"][0]
        assert item["server_boxes"] == []

    async def test_a_score_pass_does_send_the_score(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.77)
        item = (await client.get(QUEUE, headers=auth())).json()["items"][0]
        assert item["server_probability"] == pytest.approx(0.77)


class TestModelBoxesAreOptIn:
    async def _framed(self, conn):
        await _frame(conn, "f_a", score=0.5)
        await conn.execute(
            """UPDATE asset_frame SET server_detections = '[
                 {"bbox": {"x":0.1,"y":0.1,"w":0.2,"h":0.2}, "confidence":0.9},
                 {"_vlm_verdict": {"is_pothole": true, "confidence": 0.8,
                                   "severity": "deep", "rationale": "r", "model_id": "m"}}
               ]'::jsonb WHERE client_id = 'f_a'""")

    async def test_absent_by_default(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._framed(conn)
        item = (await client.get(QUEUE, headers=auth())).json()["items"][0]
        assert item["server_boxes"] == [] and item["vlm_verdict"] is None

    async def test_present_when_asked_for(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._framed(conn)
        item = (await client.get(
            QUEUE, params={"include_model_boxes": True}, headers=auth())).json()["items"][0]
        assert len(item["server_boxes"]) == 1
        assert item["vlm_verdict"]["severity"] == "deep"

    async def test_the_vlm_verdict_is_not_counted_as_a_box(self, db_pool, client):
        """It has no bbox; a renderer trusting the raw list would draw garbage."""
        async with db_pool.acquire() as conn:
            await self._framed(conn)
        item = (await client.get(
            QUEUE, params={"include_model_boxes": True}, headers=auth())).json()["items"][0]
        assert len(item["server_boxes"]) == 1


# ── Writes ────────────────────────────────────────────────────────────────────


class TestVerdicts:
    async def test_a_verdict_is_recorded_with_the_tokens_user(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        resp = await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": 1, "note": "clear edge"}, headers=auth())
        assert resp.status_code == 200
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT label, note, labeled_by FROM frame_label WHERE frame_client_id='f_a'")
        assert row["label"] == 1
        assert row["note"] == "clear edge"
        # From the JWT, never the body: otherwise a reviewer could attribute a
        # verdict to a colleague.
        assert row["labeled_by"] == "u1"

    async def test_every_verdict_appends_to_history(self, db_pool, client):
        """frame_label holds one row per frame and its write is an upsert, so a second
        annotator silently overwrites the first. History makes that recoverable."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        await client.post(f"{QUEUE}/f_a/verdict", json={"label": 1}, headers=auth())
        await client.post(f"{QUEUE}/f_a/verdict", json={"label": 0}, headers=auth())
        async with db_pool.acquire() as conn:
            labels = [r["label"] for r in await conn.fetch(
                "SELECT label FROM frame_label_history WHERE frame_client_id='f_a' "
                "ORDER BY created_at, history_id")]
            current = await conn.fetchval(
                "SELECT label FROM frame_label WHERE frame_client_id='f_a'")
        assert labels == [1, 0]
        assert current == 0

    @pytest.mark.parametrize("label", [1, 0, -1])
    async def test_unsure_is_a_decision_not_a_gap(self, db_pool, client, label):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        assert (await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": label}, headers=auth())).status_code == 200
        # Having been decided, it leaves the outstanding queue in every case.
        resp = await client.get(QUEUE, headers=auth())
        assert "f_a" not in {i["client_id"] for i in resp.json()["items"]}

    async def test_an_unknown_frame_is_404(self, client):
        assert (await client.post(
            f"{QUEUE}/nope/verdict", json={"label": 1}, headers=auth())).status_code == 404

    async def test_an_invalid_label_is_refused(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        assert (await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": 7}, headers=auth())).status_code == 400


class TestBoxesAndDrafts:
    BOX = {"class_id": 0, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}

    async def _judged(self, conn, client_id="f_a"):
        await _frame(conn, client_id, score=0.5)
        await _label(conn, client_id, 1)

    async def test_boxing_an_unjudged_frame_is_409(self, db_pool, client):
        """Not 404 — the frame exists; the caller has not met the precondition.
        Drawing a box before deciding what the frame IS anchors the verdict."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        resp = await client.post(
            f"{QUEUE}/f_a/boxes", json={"boxes": [self.BOX]}, headers=auth())
        assert resp.status_code == 409

    async def test_saving_boxes_leaves_the_frame_a_draft(self, db_pool, client):
        """Saving is not submitting: boxed_at stays NULL so the frame remains
        editable and invisible to the exporter."""
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        resp = await client.post(
            f"{QUEUE}/f_a/boxes", json={"boxes": [self.BOX]}, headers=auth())
        assert resp.status_code == 200
        assert resp.json()["boxed_at"] is None
        async with db_pool.acquire() as conn:
            row = await conn.fetchrow(
                "SELECT boxed_at, boxes_drafted_at FROM frame_label WHERE frame_client_id='f_a'")
        assert row["boxed_at"] is None
        assert row["boxes_drafted_at"] is not None

    async def test_an_empty_box_set_still_records_a_draft(self, db_pool, client):
        """The hole migrations/017 exists to close. 'I looked, there is nothing here'
        leaves no boxes, so without boxes_drafted_at it is indistinguishable from a
        frame nobody opened — and only one of those may be exported as background."""
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        await client.post(f"{QUEUE}/f_a/boxes", json={"boxes": []}, headers=auth())
        async with db_pool.acquire() as conn:
            drafted = await conn.fetchval(
                "SELECT boxes_drafted_at FROM frame_label WHERE frame_client_id='f_a'")
        assert drafted is not None

    async def test_saving_replaces_rather_than_appends(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        await client.post(f"{QUEUE}/f_a/boxes", json={"boxes": [self.BOX]}, headers=auth())
        await client.post(
            f"{QUEUE}/f_a/boxes",
            json={"boxes": [self.BOX, {**self.BOX, "x": 0.5}]}, headers=auth())
        async with db_pool.acquire() as conn:
            n = await conn.fetchval(
                "SELECT count(*) FROM frame_box WHERE frame_client_id='f_a'")
        assert n == 2

    async def test_a_draft_is_readopted_by_the_queue(self, db_pool, client):
        """An interrupted pass must be resumable: the reviewer's own boxes are never
        withheld, so reloading returns them."""
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        await client.post(f"{QUEUE}/f_a/boxes", json={"boxes": [self.BOX]}, headers=auth())
        item = (await client.get(QUEUE, params={"mode": "box"}, headers=auth())).json()["items"][0]
        assert item["client_id"] == "f_a"
        assert len(item["human_boxes"]) == 1
        assert item["boxes_drafted_at"] is not None and item["boxed_at"] is None

    async def test_a_thin_region_box_warns_but_is_saved(self, db_pool, client):
        """Box regions, not lines — a sliver is mostly undamaged asphalt. The operator
        may still mean it, so this is a warning and never a refusal."""
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        crack = {"class_id": 4, "x": 0.1, "y": 0.1, "w": 0.6, "h": 0.01}
        resp = await client.post(f"{QUEUE}/f_a/boxes", json={"boxes": [crack]}, headers=auth())
        assert resp.status_code == 200
        assert resp.json()["thin_warnings"] == ["crack"]
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM frame_box WHERE frame_client_id='f_a'") == 1

    async def test_an_invalid_box_is_400_not_500(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await self._judged(conn)
        resp = await client.post(
            f"{QUEUE}/f_a/boxes",
            json={"boxes": [{"class_id": 99, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2}]},
            headers=auth())
        assert resp.status_code == 400


class TestSubmit:
    async def test_submitting_signs_off_and_removes_from_the_queue(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _label(conn, "f_a", 1, drafted=True)
        resp = await client.post(
            f"{QUEUE}/boxes/submit", json={"client_ids": ["f_a"]}, headers=auth())
        assert resp.status_code == 200
        assert resp.json()["finalized"] == 1
        remaining = (await client.get(
            QUEUE, params={"mode": "box"}, headers=auth())).json()["items"]
        assert "f_a" not in {i["client_id"] for i in remaining}

    async def test_an_unjudged_frame_is_reported_not_silently_dropped(self, db_pool, client):
        """The marking UPDATE runs on frame_label, so an unjudged frame has no row to
        update and would vanish without explanation. The operator needs to know why
        one of their batch did not finalize."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _label(conn, "f_a", 1, drafted=True)
            await _frame(conn, "f_never", score=0.4)
        body = (await client.post(
            f"{QUEUE}/boxes/submit",
            json={"client_ids": ["f_a", "f_never"]}, headers=auth())).json()
        assert body["finalized"] == 1
        assert body["skipped_unjudged"] == ["f_never"]

    async def test_resubmitting_is_idempotent(self, db_pool, client):
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _label(conn, "f_a", 1, drafted=True)
        await client.post(f"{QUEUE}/boxes/submit", json={"client_ids": ["f_a"]}, headers=auth())
        body = (await client.post(
            f"{QUEUE}/boxes/submit", json={"client_ids": ["f_a"]}, headers=auth())).json()
        assert body["finalized"] == 0
        assert body["already_finalized"] == 1

    async def test_a_frame_nobody_boxed_cannot_be_signed_off(self, db_pool, client):
        """The one that poisons the training set if it gets through.

        scripts/export_labeled_frames.py keys on boxed_at ALONE and treats a signed-off
        frame with no boxes as "reviewed, genuinely clean" -- a YOLO BACKGROUND image.
        A frame nobody ever opened has no boxes for a completely different reason, and
        shipping it as background is the exact mechanism that took recall 0.708 -> 0.215
        across v2/v3/v4. The client sends the id list, so this guard cannot live in a
        browser.
        """
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_never", score=0.5)
            await _label(conn, "f_never", 1)          # judged, never boxed

        body = (await client.post(
            f"{QUEUE}/boxes/submit", json={"client_ids": ["f_never"]}, headers=auth())).json()

        assert body["finalized"] == 0
        assert body["skipped_undrafted"] == ["f_never"]
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT boxed_at FROM frame_label WHERE frame_client_id='f_never'") is None

    async def test_an_empty_draft_can_be_signed_off(self, db_pool, client):
        """The mirror image, and why the guard is boxes_drafted_at rather than
        "does it have boxes": a frame a human opened and found genuinely clean has
        zero boxes and IS valid background training data."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_clean", score=0.5)
            await _label(conn, "f_clean", 0)
        await client.post(f"{QUEUE}/f_clean/boxes", json={"boxes": []}, headers=auth())

        body = (await client.post(
            f"{QUEUE}/boxes/submit", json={"client_ids": ["f_clean"]}, headers=auth())).json()

        assert body["finalized"] == 1
        assert body["skipped_undrafted"] == []

    async def test_unsubmit_keeps_the_boxes(self, db_pool, client):
        """The marker says 'a human signed this off'; clearing it returns the frame to
        the queue with its work intact."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _label(conn, "f_a", 1, drafted=True)
            await conn.execute(
                "INSERT INTO frame_box (frame_client_id, class_id, x, y, w, h, labeled_by) "
                "VALUES ('f_a', 0, 0.1, 0.1, 0.2, 0.2, 'u1')")
        await client.post(f"{QUEUE}/boxes/submit", json={"client_ids": ["f_a"]}, headers=auth())
        resp = await client.post(
            f"{QUEUE}/boxes/unsubmit", json={"client_ids": ["f_a"]}, headers=auth("admin"))
        assert resp.json()["cleared"] == 1
        async with db_pool.acquire() as conn:
            assert await conn.fetchval(
                "SELECT count(*) FROM frame_box WHERE frame_client_id='f_a'") == 1
            assert await conn.fetchval(
                "SELECT boxed_at FROM frame_label WHERE frame_client_id='f_a'") is None


# ── The served class list ─────────────────────────────────────────────────────


async def test_the_queue_serves_the_class_list(client):
    """app/detection/classes.py warns that the class list, the model's data.yaml and
    DETECTION_CLASS_NAMES must agree BY POSITION, and that disagreement means
    server_probability can be sourced from the wrong class — which fusion cannot
    detect because it never sees a class. A hand-copied frontend array would be a
    fourth artefact to drift, so it is served."""
    from app.detection.classes import PRIMARY_CLASS_ID, ROAD_SURFACE_CLASSES

    body = (await client.get(QUEUE, headers=auth())).json()
    assert body["classes"] == list(ROAD_SURFACE_CLASSES)
    assert body["primary_class_id"] == PRIMARY_CLASS_ID
    assert "crack" in body["region_classes"]


# -- Who may write ground truth ------------------------------------------------


class TestWriteAuthorization:
    async def test_a_viewer_cannot_label(self, db_pool, client):
        """Reads are open to viewers; ground truth is not.

        The role is demoted in org_member, not just in the token: the write tier
        re-reads the database, so a token claiming 'viewer' over a membership saying
        'staff' would (correctly) still be allowed to write. The DB is the authority.
        """
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _make_staff(conn, role="viewer")

        assert (await client.get(QUEUE, headers=auth("viewer"))).status_code == 200
        resp = await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": 1}, headers=auth("viewer"))
        assert resp.status_code == 403

    async def test_a_revoked_reviewer_stops_writing_immediately(self, db_pool, client):
        """The whole reason writes use StaffOrAboveLive rather than the JWT claim: a
        reviewer removed from the org must stop writing training data now, not within
        the access token's remaining TTL. The token below stays valid throughout."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
        assert (await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": 1}, headers=auth())).status_code == 200

        async with db_pool.acquire() as conn:
            await conn.execute("DELETE FROM org_member WHERE user_id = 'u1'")

        assert (await client.post(
            f"{QUEUE}/f_a/verdict", json={"label": 0}, headers=auth())).status_code == 403

    async def test_unsubmit_takes_an_admin(self, db_pool, client):
        """It retracts an attestation that may be somebody else's."""
        async with db_pool.acquire() as conn:
            await _frame(conn, "f_a", score=0.5)
            await _label(conn, "f_a", 1, boxed=True)
        assert (await client.post(
            f"{QUEUE}/boxes/unsubmit",
            json={"client_ids": ["f_a"]}, headers=auth("staff"))).status_code == 403
        assert (await client.post(
            f"{QUEUE}/boxes/unsubmit",
            json={"client_ids": ["f_a"]}, headers=auth("admin"))).status_code == 200
