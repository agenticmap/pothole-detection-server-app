"""Parsing a detections column into boxes, and keeping non-boxes out of them.

The failure this guards is specific and silent. `asset_frame.server_detections` holds
the detector's boxes, but under DETECTION_BACKEND=hybrid app/detection/hybrid_v1.py
APPENDS a {"_vlm_verdict": {...}} element to the same list. That element has no bbox.
A renderer that trusts the list produces a KeyError or draws garbage, and
jsonb_array_length() over it reports one box too many on every verified frame.

The filter is structural (does this entry carry a dict bbox?) rather than a special
case on the key name, so it also excludes whatever non-box annotation is appended next.
That property is what the last test in this file pins.
"""

import json

from app.services.detection_boxes import (
    count_detection_boxes,
    parse_detection_boxes,
    parse_vlm_verdict,
)


def box(x=0.1, y=0.2, w=0.3, h=0.4, **extra):
    return {"bbox": {"x": x, "y": y, "w": w, "h": h}, "confidence": 0.9, **extra}


VLM_ELEMENT = {
    "_vlm_verdict": {
        "is_pothole": True,
        "confidence": 0.8,
        "severity": "deep",
        "rationale": "Clear depression with broken edges.",
        "model_id": "qwen2.5vl:7b",
    }
}


class TestBoxParsing:
    def test_a_plain_list_of_boxes(self):
        out = parse_detection_boxes([box(), box(x=0.5)])
        assert [b["x"] for b in out] == [0.1, 0.5]
        assert out[0]["confidence"] == 0.9

    def test_asyncpg_hands_jsonb_back_as_text(self):
        """The column arrives as a str, not a list, depending on the caller."""
        assert len(parse_detection_boxes(json.dumps([box(), box()]))) == 2

    def test_label_and_class_id_ride_along_when_present(self):
        out = parse_detection_boxes([box(label="pothole", class_id=0)])
        assert out[0]["label"] == "pothole"
        assert out[0]["class_id"] == 0

    def test_a_missing_label_is_absent_not_invented(self):
        out = parse_detection_boxes([box()])
        assert "label" not in out[0] and "class_id" not in out[0]

    def test_a_bool_is_not_a_class_id(self):
        """True == 1 in Python, so an unguarded isinstance check would call a boolean
        class 1 -- which is 'manhole' in the five-class set."""
        assert "class_id" not in parse_detection_boxes([box(class_id=True)])[0]


class TestJunkIsDroppedNotGuessedAt:
    def test_none_and_empty(self):
        assert parse_detection_boxes(None) == []
        assert parse_detection_boxes("") == []
        assert parse_detection_boxes("[]") == []
        assert parse_detection_boxes([]) == []

    def test_malformed_json(self):
        assert parse_detection_boxes("{not json") == []

    def test_a_non_list_payload(self):
        assert parse_detection_boxes('{"bbox": {"x": 0}}') == []

    def test_a_string_coordinate_is_coerced_not_crashed(self):
        out = parse_detection_boxes([{"bbox": {"x": "0.1", "y": "0.2", "w": "0.3", "h": "0.4"}}])
        assert out[0]["x"] == 0.1

    def test_an_uncoercible_coordinate_drops_only_that_box(self):
        out = parse_detection_boxes([box(), {"bbox": {"x": "wide", "y": 0, "w": 1, "h": 1}}])
        assert len(out) == 1

    def test_a_bbox_missing_a_side_is_dropped(self):
        assert parse_detection_boxes([{"bbox": {"x": 0.1, "y": 0.2, "w": 0.3}}]) == []


class TestTheVlmElement:
    def test_it_is_not_a_box(self):
        """The whole point: two boxes plus a verdict is two boxes."""
        out = parse_detection_boxes([box(), VLM_ELEMENT, box(x=0.5)])
        assert len(out) == 2

    def test_it_is_surfaced_separately(self):
        v = parse_vlm_verdict([box(), VLM_ELEMENT])
        assert v is not None
        assert v["is_pothole"] is True
        assert v["severity"] == "deep"
        assert v["model_id"] == "qwen2.5vl:7b"

    def test_absent_when_no_vlm_ran(self):
        assert parse_vlm_verdict([box(), box()]) is None
        assert parse_vlm_verdict(None) is None

    def test_the_count_matches_the_boxes(self):
        """count_detection_boxes is what the frames tile's server_box_count must agree
        with; jsonb_array_length() over the same payload would say 3."""
        payload = [box(), VLM_ELEMENT, box(x=0.5)]
        assert count_detection_boxes(payload) == 2
        assert len(payload) == 3

    def test_the_filter_is_structural_not_a_name_check(self):
        """A future non-box annotation with a different key must also be excluded
        without editing the parser."""
        out = parse_detection_boxes([box(), {"_road_surface": {"kind": "gravel"}}])
        assert len(out) == 1
