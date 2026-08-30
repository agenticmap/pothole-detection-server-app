"""Unit tests for the ONNX detector's geometry and decode (Phase 2.7).

Before this file, app/detection/onnx_v1.py had **never executed** — every other
test injects a stub detector, so the letterbox, the [1, 4+nc, N] decode, NMS and
the box back-mapping were unverified. A wrong coordinate hop here does not raise;
it writes plausible-looking boxes into server_detections forever.

No model file is needed: onnxruntime.InferenceSession is monkeypatched with a fake
that returns a crafted output tensor, so the arithmetic is checked exactly.

Frame size is the real one — 480x640 portrait, as uploaded by the app.
"""

import io
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from app.detection.onnx_v1 import OnnxYoloDetector

FRAME_W, FRAME_H = 480, 640


class FakeSession:
    """Stands in for ort.InferenceSession: records the fed tensor, returns a fixture."""

    def __init__(self, output, shapeless=False):
        self._output = output
        self.shapeless = shapeless
        self.last_tensor = None

    def get_inputs(self):
        return [SimpleNamespace(name="images")]

    def get_outputs(self):
        """Real exports declare a static shape; the ctor guard reads it to fail at boot.

        `shapeless=True` models a dynamic export, where the check has to defer to
        `_check_layout` on the first frame instead.
        """
        if self.shapeless:
            return [SimpleNamespace(shape=["batch", "channels", "anchors"])]
        return [SimpleNamespace(shape=list(self._output.shape))]

    def run(self, _outputs, feed):
        self.last_tensor = next(iter(feed.values()))
        return [self._output]


def _output(*boxes):
    """Build a raw Ultralytics [1, 4+nc, N] tensor from (cx, cy, w, h, score) tuples.

    Padded to 8 anchors: the layout guard requires the channel axis to be the small
    one, and a 1-anchor tensor would (correctly) look like a transposed export.
    """
    n = max(len(boxes), 8)
    arr = np.zeros((1, 5, n), dtype=np.float32)
    for i, (cx, cy, w, h, score) in enumerate(boxes):
        arr[0, :, i] = [cx, cy, w, h, score]
    return arr


def _multi(*boxes, nc=4):
    """Build a raw [1, 4+nc, N] tensor from (cx, cy, w, h, {class_id: score}) tuples.

    The single-class `_output` cannot express the case that matters in Phase 2.7b:
    two boxes of different classes competing to be the frame probability.
    """
    n = max(len(boxes), 8)
    arr = np.zeros((1, 4 + nc, n), dtype=np.float32)
    for i, (cx, cy, w, h, scores) in enumerate(boxes):
        arr[0, :4, i] = [cx, cy, w, h]
        for class_id, score in scores.items():
            arr[0, 4 + class_id, i] = score
    return arr


ROAD_CLASSES = ("pothole", "manhole", "grate", "patch")


def _jpeg(width=FRAME_W, height=FRAME_H, road_band=None):
    """A flat grey frame. `road_band=(top, bottom)` paints that slice red so a test
    can count how much of it survives into the model input."""
    from PIL import Image

    img = Image.new("RGB", (width, height), (100, 110, 120))
    if road_band is not None:
        top, bottom = road_band
        band = Image.new("RGB", (width, int(round(height * (bottom - top)))), (220, 30, 30))
        img.paste(band, (0, int(round(height * top))))
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue()


def _detector(monkeypatch, output, **kw):
    """Build a detector over a fake session, with no onnxruntime install required.

    onnx_v1 does `import onnxruntime as ort` inside __init__, so a stub module in
    sys.modules satisfies it. setitem (not setattr on the real package) means this
    works whether or not onnxruntime is installed, and monkeypatch reverts it — the
    decode arithmetic is what is under test, not the runtime.
    """
    session = FakeSession(output, shapeless=kw.pop("shapeless", False))
    monkeypatch.setitem(
        sys.modules,
        "onnxruntime",
        SimpleNamespace(InferenceSession=lambda *a, **k: session),
    )
    kw.setdefault("model_path", "fake.onnx")
    kw.setdefault("model_id", "test_v1")
    return OnnxYoloDetector(**kw), session


# A box that, with the ROI crop OFF, lands exactly on the middle 10% of the frame.
# Letterbox for 480x640 → ratio 1.0, pad dw=80, dh=0. So full-frame centre (240,320)
# sits at (320, 320) in letterbox space.
CENTRE_BOX = (320.0, 320.0, 48.0, 64.0, 0.9)


class TestGeometry:
    def test_box_maps_back_to_full_frame_normalized_coords(self, monkeypatch):
        det, _ = _detector(monkeypatch, _output(CENTRE_BOX))
        result = det.detect(_jpeg())

        assert result.probability == pytest.approx(0.9)
        assert len(result.detections) == 1
        bbox = result.detections[0]["bbox"]
        # (216,288)-(264,352) px → normalized against 480x640.
        assert bbox["x"] == pytest.approx(0.45)
        assert bbox["y"] == pytest.approx(0.45)
        assert bbox["w"] == pytest.approx(0.10)
        assert bbox["h"] == pytest.approx(0.10)

    def test_roi_crop_shifts_the_box_by_the_crop_offset(self, monkeypatch):
        """The identical tensor must decode to a different place once ROI is on.

        This is the hop that silently breaks: crop 0.45-0.90 of a 640px frame keeps
        rows 288-576, so the letterbox ratio becomes 640/480 = 1.333 and every y
        needs +288 added back. Getting it wrong shifts every box up by 288px.
        """
        det, _ = _detector(
            monkeypatch, _output(CENTRE_BOX), roi_enabled=True, roi_top=0.45, roi_bottom=0.90
        )
        bbox = det.detect(_jpeg()).detections[0]["bbox"]
        # ratio 4/3, dh=128 → full-frame (222,408)-(258,456).
        assert bbox["x"] == pytest.approx(222 / FRAME_W)
        assert bbox["y"] == pytest.approx(408 / FRAME_H)
        assert bbox["w"] == pytest.approx(36 / FRAME_W)
        assert bbox["h"] == pytest.approx(48 / FRAME_H)

    def test_roi_crop_feeds_more_road_pixels(self, monkeypatch):
        """The whole justification for the crop, measured.

        Not "fewer padding pixels" — cropping *reduces* total content, since it
        throws the sky away. The claim is narrower and is the one that matters: the
        road band itself lands on more of the 640x640 input. Marking the band in the
        source image and counting it in the fed tensor measures exactly that.
        """
        marked = _jpeg(road_band=(0.45, 0.90))

        def road_pixels(tensor):
            img = tensor[0].transpose(1, 2, 0)  # 640,640,3 in 0..1
            red, green = img[:, :, 0], img[:, :, 1]
            return int(np.sum((red > 0.6) & (green < 0.35)))

        off, s_off = _detector(monkeypatch, _output(CENTRE_BOX))
        off.detect(marked)
        on, s_on = _detector(
            monkeypatch, _output(CENTRE_BOX), roi_enabled=True, roi_top=0.45, roi_bottom=0.90
        )
        on.detect(marked)

        # 288x480 of band letterboxed whole-frame vs 384x640 cropped → ~1.78x.
        assert road_pixels(s_on.last_tensor) > 1.5 * road_pixels(s_off.last_tensor)

    def test_boxes_are_clamped_to_the_frame(self, monkeypatch):
        """A box running off the edge must not yield x+w > 1 for a downstream viewer."""
        det, _ = _detector(monkeypatch, _output((320.0, 320.0, 2000.0, 4000.0, 0.8)))
        bbox = det.detect(_jpeg()).detections[0]["bbox"]
        assert bbox["x"] == pytest.approx(0.0)
        assert bbox["y"] == pytest.approx(0.0)
        assert bbox["x"] + bbox["w"] <= 1.0 + 1e-9
        assert bbox["y"] + bbox["h"] <= 1.0 + 1e-9

    def test_degenerate_roi_falls_back_to_the_full_frame(self, monkeypatch):
        """A crop thinner than 2 rows would feed an empty tensor; use the frame instead."""
        det, _ = _detector(
            monkeypatch, _output(CENTRE_BOX), roi_enabled=True, roi_top=0.5, roi_bottom=0.5005
        )
        bbox = det.detect(_jpeg()).detections[0]["bbox"]
        assert bbox["y"] == pytest.approx(0.45)  # the no-crop answer

    def test_inverted_roi_bounds_are_rejected_at_construction(self, monkeypatch):
        with pytest.raises(ValueError, match="detection_roi_top/bottom"):
            _detector(monkeypatch, _output(CENTRE_BOX), roi_enabled=True, roi_top=0.9,
                      roi_bottom=0.4)


class TestDecode:
    def test_scores_below_the_threshold_are_dropped(self, monkeypatch):
        det, _ = _detector(monkeypatch, _output((320.0, 320.0, 48.0, 64.0, 0.10)))
        result = det.detect(_jpeg())
        assert result.detections == []
        # 0.0, not None: the detector ran and found nothing, which is a real answer.
        assert result.probability == 0.0

    def test_nms_collapses_overlapping_boxes(self, monkeypatch):
        det, _ = _detector(
            monkeypatch,
            _output(
                (320.0, 320.0, 48.0, 64.0, 0.9),
                (322.0, 322.0, 48.0, 64.0, 0.8),  # ~92% IoU with the first
            ),
        )
        result = det.detect(_jpeg())
        assert len(result.detections) == 1
        assert result.detections[0]["confidence"] == pytest.approx(0.9)

    def test_distant_boxes_both_survive(self, monkeypatch):
        det, _ = _detector(
            monkeypatch,
            _output((160.0, 200.0, 40.0, 40.0, 0.9), (480.0, 500.0, 40.0, 40.0, 0.7)),
        )
        assert len(det.detect(_jpeg()).detections) == 2

    def test_probability_is_the_best_box(self, monkeypatch):
        det, _ = _detector(
            monkeypatch,
            _output((160.0, 200.0, 40.0, 40.0, 0.42), (480.0, 500.0, 40.0, 40.0, 0.77)),
        )
        assert det.detect(_jpeg()).probability == pytest.approx(0.77)

    def test_class_id_comes_from_the_argmax(self, monkeypatch):
        arr = np.zeros((1, 7, 8), dtype=np.float32)  # 4 + 3 classes
        arr[0, :4, 0] = [320.0, 320.0, 48.0, 64.0]
        arr[0, 4:, 0] = [0.1, 0.2, 0.8]  # class 2 wins
        det, _ = _detector(monkeypatch, arr, labels=("pothole", "manhole", "grate"))
        det_out = det.detect(_jpeg()).detections[0]
        assert det_out["class_id"] == 2
        assert det_out["confidence"] == pytest.approx(0.8)


class TestOutputLayoutGuard:
    """A wrong export decodes to garbage rather than raising — so it must be caught."""

    def test_nms_baked_export_is_rejected(self, monkeypatch):
        det, _ = _detector(monkeypatch, np.zeros((1, 300, 6), dtype=np.float32))
        with pytest.raises(ValueError, match="nms=False"):
            det.detect(_jpeg())

    def test_transposed_export_is_rejected(self, monkeypatch):
        det, _ = _detector(monkeypatch, np.zeros((1, 8400, 5), dtype=np.float32))
        with pytest.raises(ValueError, match="not the raw Ultralytics"):
            det.detect(_jpeg())

    def test_wrong_rank_is_rejected(self, monkeypatch):
        det, _ = _detector(monkeypatch, np.zeros((5, 8400), dtype=np.float32))
        with pytest.raises(ValueError, match="expected"):
            det.detect(_jpeg())


class TestContract:
    def test_detection_shape_matches_what_the_device_sends(self, monkeypatch):
        """server_detections and device_detections are one column family, one shape.

        The real device payload is verified against pothole_db:
        {"bbox": {"x","y","w","h"}, "label", "class_id", "confidence"}.
        """
        det, _ = _detector(monkeypatch, _output(CENTRE_BOX), labels=("pothole",))
        d = det.detect(_jpeg()).detections[0]
        assert set(d) == {"bbox", "label", "class_id", "confidence"}
        assert set(d["bbox"]) == {"x", "y", "w", "h"}
        assert d["label"] == "pothole"
        assert all(0.0 <= v <= 1.0 for v in d["bbox"].values())

    def test_model_id_and_version_are_reported(self, monkeypatch):
        det, _ = _detector(monkeypatch, _output(CENTRE_BOX), model_id="yolo11s_pothole_v1")
        result = det.detect(_jpeg())
        assert result.model_id == "yolo11s_pothole_v1"
        assert result.version == "detection.onnx_yolo_v2"

    def test_empty_model_path_is_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="detection_model_path is required"):
            _detector(monkeypatch, _output(CENTRE_BOX), model_path="")

    def test_landscape_frames_still_work(self, monkeypatch):
        """Not every device is portrait; the maths must not assume it."""
        det, _ = _detector(monkeypatch, _output((320.0, 320.0, 64.0, 64.0, 0.9)))
        bbox = det.detect(_jpeg(width=640, height=480)).detections[0]["bbox"]
        assert 0.0 <= bbox["x"] <= 1.0
        assert 0.0 <= bbox["y"] <= 1.0


class TestClassAwareProbability:
    """Phase 2.7b. Fusion blends `probability` with no notion of class, so the number
    must describe the *pothole* class alone — see app/fusion/service.py."""

    def test_a_confident_manhole_does_not_become_a_confident_pothole(self, monkeypatch):
        """The regression this guard exists for.

        Before the fix `probability` was `max` over every class, so this frame
        reported 0.93 — and fusion, which cannot tell a manhole from a pothole,
        would have read it as a confirmed pothole.
        """
        det, _ = _detector(
            monkeypatch,
            _multi(
                (160.0, 200.0, 40.0, 40.0, {0: 0.41}),  # a weak pothole
                (400.0, 480.0, 40.0, 40.0, {1: 0.93}),  # a confident manhole
            ),
            labels=ROAD_CLASSES,
        )
        result = det.detect(_jpeg())
        assert result.probability == pytest.approx(0.41)
        # Both boxes are still reported; only the frame scalar is class-filtered.
        assert {d["class_id"] for d in result.detections} == {0, 1}

    def test_only_other_classes_scores_zero(self, monkeypatch):
        """0.0, not None: the detector ran and found no pothole.

        NULLIF(server_probability, 0.0) then treats it as *no measurement* and falls
        back to device_probability, which is the intended reading — "that is a
        manhole" is not evidence about a pothole in either direction.
        """
        det, _ = _detector(
            monkeypatch,
            _multi((400.0, 480.0, 40.0, 40.0, {2: 0.88})),
            labels=ROAD_CLASSES,
        )
        result = det.detect(_jpeg())
        assert result.probability == 0.0
        assert len(result.detections) == 1

    def test_the_best_pothole_wins_not_the_first(self, monkeypatch):
        det, _ = _detector(
            monkeypatch,
            _multi(
                (160.0, 200.0, 40.0, 40.0, {0: 0.31}),
                (400.0, 480.0, 40.0, 40.0, {0: 0.62}),
            ),
            labels=ROAD_CLASSES,
        )
        assert det.detect(_jpeg()).probability == pytest.approx(0.62)

    def test_primary_class_id_selects_which_class_is_reported(self, monkeypatch):
        """The setting is real, not decorative: point it at manhole and the manhole scores."""
        det, _ = _detector(
            monkeypatch,
            _multi(
                (160.0, 200.0, 40.0, 40.0, {0: 0.41}),
                (400.0, 480.0, 40.0, 40.0, {1: 0.93}),
            ),
            labels=ROAD_CLASSES,
            primary_class_id=1,
        )
        assert det.detect(_jpeg()).probability == pytest.approx(0.93)

    def test_label_follows_the_class_id(self, monkeypatch):
        det, _ = _detector(
            monkeypatch,
            _multi(
                (160.0, 200.0, 40.0, 40.0, {0: 0.80}),
                (400.0, 480.0, 40.0, 40.0, {3: 0.70}),
            ),
            labels=ROAD_CLASSES,
        )
        by_class = {d["class_id"]: d["label"] for d in det.detect(_jpeg()).detections}
        assert by_class == {0: "pothole", 3: "patch"}

    def test_single_class_behaviour_is_unchanged_by_default(self, monkeypatch):
        """Every existing export must keep loading and scoring exactly as before."""
        det, _ = _detector(monkeypatch, _output(CENTRE_BOX))
        result = det.detect(_jpeg())
        assert result.probability == pytest.approx(0.9)
        assert result.detections[0]["label"] == "pothole"


class TestClassCountGuard:
    """A labels/nc mismatch mislabels every box AND scores the wrong class into
    server_probability. It must be loud, and early."""

    def test_mismatch_is_rejected_at_construction(self, monkeypatch):
        with pytest.raises(ValueError, match="class name"):
            _detector(monkeypatch, _multi((1.0, 1.0, 1.0, 1.0, {0: 0.5})), labels=("pothole",))

    def test_mismatch_is_rejected_at_decode_when_the_export_is_dynamic(self, monkeypatch):
        """A dynamic export declares no channel count, so the ctor cannot check it.
        _check_layout has the real array and catches it on the first frame."""
        det, _ = _detector(
            monkeypatch,
            _multi((1.0, 1.0, 1.0, 1.0, {0: 0.5})),
            labels=("pothole",),
            shapeless=True,
        )
        with pytest.raises(ValueError, match="class name"):
            det.detect(_jpeg())

    def test_a_wrong_export_still_reports_the_export_problem_first(self, monkeypatch):
        """An NMS-baked export is not a class-count problem; the message must not
        send someone off to edit DETECTION_CLASS_NAMES."""
        det, _ = _detector(monkeypatch, np.zeros((1, 300, 6), dtype=np.float32))
        with pytest.raises(ValueError, match="nms=False"):
            det.detect(_jpeg())

    def test_primary_class_id_must_index_the_labels(self, monkeypatch):
        with pytest.raises(ValueError, match="primary_class_id"):
            _detector(
                monkeypatch,
                _output(CENTRE_BOX),
                labels=("pothole",),
                primary_class_id=2,
            )

    def test_labels_cannot_be_empty(self, monkeypatch):
        with pytest.raises(ValueError, match="at least one class"):
            _detector(monkeypatch, _output(CENTRE_BOX), labels=())
