"""The corner-origin <-> YOLO centre-origin conversion, which has exactly one home.

`frame_box`, `device_detections` and `server_detections` all store corner-origin
normalized 0..1. YOLO's .txt format wants the centre. Getting that backwards does not
crash and does not raise: it trains a model on boxes offset by half their own size,
which reads as a mediocre model rather than as a bug. This is the test that makes it
read as a bug.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "export_labeled_frames",
    Path(__file__).resolve().parent.parent / "scripts" / "export_labeled_frames.py",
)
export_labeled_frames = importlib.util.module_from_spec(_SPEC)
sys.modules["export_labeled_frames"] = export_labeled_frames
_SPEC.loader.exec_module(export_labeled_frames)

_from_yolo = export_labeled_frames._from_yolo
_label_text = export_labeled_frames._label_text
_to_yolo = export_labeled_frames._to_yolo
_write_data_yaml = export_labeled_frames._write_data_yaml


@pytest.mark.parametrize(
    "box",
    [
        {"class_id": 0, "x": 0.10, "y": 0.20, "w": 0.30, "h": 0.40},
        {"class_id": 1, "x": 0.00, "y": 0.00, "w": 1.00, "h": 1.00},  # whole frame
        {"class_id": 3, "x": 0.95, "y": 0.95, "w": 0.05, "h": 0.05},  # bottom-right corner
        {"class_id": 2, "x": 0.4321, "y": 0.8765, "w": 0.0123, "h": 0.0456},  # tiny
    ],
)
def test_corner_to_centre_round_trips(box):
    class_id, cx, cy, w, h = _to_yolo(box)
    assert _from_yolo(class_id, cx, cy, w, h) == pytest.approx(box)


def test_the_centre_really_is_the_centre():
    """Guards against the conversion being a no-op, which a round-trip alone would miss."""
    _, cx, cy, w, h = _to_yolo({"class_id": 0, "x": 0.2, "y": 0.4, "w": 0.4, "h": 0.2})
    assert (cx, cy) == pytest.approx((0.4, 0.5))
    assert (w, h) == pytest.approx((0.4, 0.2))


def test_label_file_is_one_line_per_box():
    text = _label_text(
        [
            {"class_id": 0, "x": 0.1, "y": 0.1, "w": 0.2, "h": 0.2},
            {"class_id": 2, "x": 0.5, "y": 0.5, "w": 0.1, "h": 0.1},
        ]
    )
    lines = text.strip().split("\n")
    assert len(lines) == 2
    assert lines[0].split()[0] == "0"
    assert lines[1].split()[0] == "2"
    # Every coordinate a detector trains on must be a fraction, never a pixel.
    assert all(0.0 <= float(v) <= 1.0 for v in lines[0].split()[1:])


def test_no_boxes_is_an_empty_file():
    """That is how YOLO spells "this image contains nothing" -- and it is only
    truthful for a frame a human actually reviewed."""
    assert _label_text([]) == ""


def test_data_yaml_names_are_ordered_by_class_id(tmp_path):
    from app.detection.classes import ROAD_SURFACE_CLASSES

    path = _write_data_yaml(tmp_path / "train", list(ROAD_SURFACE_CLASSES))
    text = path.read_text(encoding="utf-8")
    assert f"nc: {len(ROAD_SURFACE_CLASSES)}" in text
    for idx, name in enumerate(ROAD_SURFACE_CLASSES):
        assert f"  {idx}: {name}" in text


def test_the_class_list_matches_what_the_decoder_would_load():
    """One list, three consumers. A silent disagreement here means the trained model
    scores the wrong class into server_probability, and fusion cannot notice because
    it never sees a class."""
    from app.detection.classes import PRIMARY_CLASS_ID, ROAD_SURFACE_CLASSES

    assert ROAD_SURFACE_CLASSES[PRIMARY_CLASS_ID] == "pothole"
    assert len(set(ROAD_SURFACE_CLASSES)) == len(ROAD_SURFACE_CLASSES)


def test_region_classes_are_real_classes():
    """A typo here would silently disable the thin-box warning rather than fail."""
    from app.detection.classes import REGION_CLASSES, ROAD_SURFACE_CLASSES

    assert REGION_CLASSES
    assert REGION_CLASSES <= set(ROAD_SURFACE_CLASSES)


class TestThinBoxWarning:
    """`crack` is the one class with no natural compact extent. A sliver box teaches the
    model that mostly-undamaged asphalt IS the class -- the v2/v3 suppression failure
    aimed at a new target. The warning is advisory, so this only checks the geometry."""

    def test_a_hairline_crack_line_is_thin(self):
        from app.detection.classes import is_thin

        # A crack running most of the frame width, a couple of pixels tall.
        assert is_thin(0.80, 0.02)
        assert is_thin(0.02, 0.80)  # and the same crack running vertically

    def test_an_alligator_cracked_patch_is_not_thin(self):
        from app.detection.classes import is_thin

        assert not is_thin(0.30, 0.20)
        assert not is_thin(0.40, 0.10)  # 4:1 -- wide, but still a region

    def test_the_threshold_is_where_it_is_documented(self):
        from app.detection.classes import THIN_ASPECT_RATIO, is_thin

        assert THIN_ASPECT_RATIO == 6.0
        assert not is_thin(0.60, 0.10)  # exactly 6:1 passes
        assert is_thin(0.61, 0.10)  # just over does not

    def test_a_zero_dimension_box_is_not_reported_as_thin(self):
        """It cannot reach the database anyway (CHECK w > 0), and dividing by it here
        would raise inside a warning path -- turning advice into a crash."""
        from app.detection.classes import is_thin

        assert not is_thin(0.5, 0.0)
