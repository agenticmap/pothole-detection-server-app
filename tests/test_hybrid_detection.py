"""Unit tests for the hybrid detector (Stage-1 + VLM verifier) and verdict parsing.

Pure unit tests — no Postgres, no onnxruntime, no cloud SDK. A StubStage1Detector
and StubVlmVerifier exercise the gray-zone gate, the logit blend direction, the
per-run call cap, crop on/off, and graceful VLM failure. parse_verdict is tested
against fenced / prose-wrapped / malformed replies.
"""

import io

import pytest

from app.detection.engine import DetectionResult
from app.detection.hybrid_v1 import MIN_CROP_PX, HybridDetector
from app.detection.vlm.base import VlmVerdict, parse_verdict

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


class StubStage1Detector:
    version = "detection.stub_stage1"

    def __init__(self, probability, detections=None, model_id="yolo_stub"):
        self._p = probability
        self._d = detections if detections is not None else []
        self.model_id = model_id

    def detect(self, jpeg: bytes) -> DetectionResult:
        return DetectionResult(
            probability=self._p, detections=list(self._d), model_id=self.model_id,
            version=self.version,
        )


class StubVlmVerifier:
    version = "vlm.stub"

    def __init__(self, verdict=None, raises=False):
        self._verdict = verdict
        self._raises = raises
        self.calls = 0
        self.last_image = None

    def verify(self, image: bytes, context: dict) -> VlmVerdict:
        self.calls += 1
        self.last_image = image
        if self._raises:
            raise RuntimeError("boom")
        return self._verdict


def _hybrid(stage1, verifier, **kw):
    kw.setdefault("crop", False)  # default off so unit tests don't need Pillow
    return HybridDetector(stage1=stage1, verifier=verifier, **kw)


def test_high_confidence_skips_vlm():
    v = StubVlmVerifier(VlmVerdict(False, 0.9, None, "no", "stub"))
    det = _hybrid(StubStage1Detector(0.9), v, low=0.4, high=0.75)
    out = det.detect(JPEG)
    assert v.calls == 0
    assert out.probability == 0.9  # passthrough, unchanged


def test_clear_negative_skips_vlm():
    v = StubVlmVerifier(VlmVerdict(True, 0.9, "deep", "yes", "stub"))
    det = _hybrid(StubStage1Detector(0.2), v, low=0.4, high=0.75)
    out = det.detect(JPEG)
    assert v.calls == 0
    assert out.probability == 0.2


def test_none_probability_skips_vlm():
    v = StubVlmVerifier(VlmVerdict(True, 0.9, "deep", "yes", "stub"))
    det = _hybrid(StubStage1Detector(None), v)
    out = det.detect(JPEG)
    assert v.calls == 0
    assert out.probability is None


def test_gray_zone_positive_pushes_up_and_records_verdict():
    v = StubVlmVerifier(VlmVerdict(True, 0.95, "deep", "clear pothole", "claude-x"))
    det = _hybrid(
        StubStage1Detector(0.5, detections=[{"conf": 0.5, "xywh": [10, 10, 4, 4]}]),
        v, low=0.4, high=0.75, blend_weight=0.7,
    )
    out = det.detect(JPEG)
    assert v.calls == 1
    assert out.probability > 0.5  # VLM "yes" pulls confidence up
    # original detection preserved + verdict appended
    assert out.detections[0]["conf"] == 0.5
    verdict = out.detections[-1]["_vlm_verdict"]
    assert verdict["is_pothole"] is True
    assert verdict["severity"] == "deep"
    assert verdict["rationale"] == "clear pothole"
    assert out.model_id == "yolo_stub+claude-x"


def test_gray_zone_negative_pushes_down():
    v = StubVlmVerifier(VlmVerdict(False, 0.95, None, "just a shadow", "claude-x"))
    det = _hybrid(StubStage1Detector(0.6), v, low=0.4, high=0.75, blend_weight=0.7)
    out = det.detect(JPEG)
    assert v.calls == 1
    assert out.probability < 0.6  # VLM "no" pulls confidence down


def test_call_cap_falls_back_to_stage1():
    v = StubVlmVerifier(VlmVerdict(True, 0.9, "medium", "yes", "stub"))
    det = _hybrid(StubStage1Detector(0.5), v, low=0.4, high=0.75, max_calls=1)
    first = det.detect(JPEG)
    second = det.detect(JPEG)
    assert v.calls == 1                 # cap honored
    assert first.probability != 0.5     # first frame got the VLM blend
    assert second.probability == 0.5    # second fell back to Stage-1 probability


def test_vlm_failure_falls_back_to_stage1():
    v = StubVlmVerifier(raises=True)
    det = _hybrid(StubStage1Detector(0.5), v, low=0.4, high=0.75)
    out = det.detect(JPEG)
    assert v.calls == 1
    assert out.probability == 0.5  # exception swallowed → Stage-1 probability


def test_no_verifier_passes_through():
    det = _hybrid(StubStage1Detector(0.5), None, low=0.4, high=0.75)
    out = det.detect(JPEG)
    assert out.probability == 0.5


def test_crop_disabled_sends_full_frame():
    v = StubVlmVerifier(VlmVerdict(True, 0.8, "shallow", "yes", "stub"))
    det = _hybrid(StubStage1Detector(0.5, detections=[_box(0.1, 0.1, 0.2, 0.2)]), v, crop=False)
    det.detect(JPEG)
    assert v.last_image == JPEG  # untouched bytes


# ── parse_verdict ───────────────────────────────────────────────────────────


def test_parse_plain_json():
    text = '{"is_pothole": true, "confidence": 0.8, "severity": "deep", "rationale": "x"}'
    v = parse_verdict(text, "m")
    assert v.is_pothole is True and v.confidence == 0.8 and v.severity == "deep"
    assert v.model_id == "m"


def test_parse_strips_code_fence_and_prose():
    text = (
        'Sure!\n```json\n'
        '{"is_pothole": false, "confidence": 0.3, "severity": "none", "rationale": "shadow"}\n```'
    )
    v = parse_verdict(text, "m")
    assert v.is_pothole is False
    assert v.severity is None  # "none" → None


def test_parse_clamps_confidence_and_drops_bad_severity():
    text = '{"is_pothole": true, "confidence": 1.7, "severity": "huge", "rationale": "x"}'
    v = parse_verdict(text, "m")
    assert v.confidence == 1.0      # clamped to [0,1]
    assert v.severity is None       # invalid enum → None


def test_parse_negative_forces_severity_none():
    text = '{"is_pothole": false, "confidence": 0.9, "severity": "deep", "rationale": "x"}'
    v = parse_verdict(text, "m")
    assert v.is_pothole is False
    assert v.severity is None  # severity meaningless for a negative verdict


def test_parse_raises_on_no_json():
    with pytest.raises(ValueError):
        parse_verdict("I cannot help with that.", "m")


# ── _crop (Phase 2.7) ─────────────────────────────────────────────────────────
#
# This path had zero coverage: every test above sets crop=False so Pillow isn't
# needed. It matters now for two reasons — the box shape changed to match the
# device's normalized corner-origin form, and the VLM step that follows relies on
# the crop being right, since a wrong crop sends the verifier a picture of the
# wrong part of the road and the verdict looks plausible either way.


def _box(x, y, w, h, conf=0.5):
    """A detection in the shape onnx_v1 emits: normalized, corner-origin."""
    return {
        "bbox": {"x": x, "y": y, "w": w, "h": h},
        "label": "pothole",
        "class_id": 0,
        "confidence": conf,
    }


def _jpeg(width=200, height=100):
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (width, height), (120, 120, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _size(jpeg_bytes):
    from PIL import Image

    return Image.open(io.BytesIO(jpeg_bytes)).size


def _crop_via_detect(detections, *, crop_margin=0.0, image=None):
    """Drive _crop through detect() so the gray-zone gate is exercised too."""
    image = image or _jpeg()
    verifier = StubVlmVerifier(VlmVerdict(True, 0.9, "high", "r", "m"))
    det = _hybrid(
        StubStage1Detector(0.5, detections=detections),
        verifier,
        crop=True,
        crop_margin=crop_margin,
    )
    det.detect(image)
    assert verifier.calls == 1
    return verifier.last_image


def test_crop_takes_the_union_of_every_box():
    # 800x400 image. Box A → px (80,80)-(240,200); box B → px (400,240)-(480,320).
    # Union is (80,80)-(480,320), i.e. 400x240. Neither box alone gives that.
    #
    # Deliberately larger than the 200x100 default: on that frame the union is
    # 100x60 and MIN_CROP_PX would widen the height to 64, which is correct
    # behaviour but masks the union arithmetic this test exists to check. A real
    # frame is 480x640, so the larger size is also the more representative one.
    cropped = _crop_via_detect(
        [_box(0.1, 0.2, 0.2, 0.3), _box(0.5, 0.6, 0.1, 0.2)], image=_jpeg(800, 400)
    )
    assert _size(cropped) == (400, 240)


def test_crop_applies_the_margin_on_all_four_sides():
    # Same union (100x60). margin=0.2 → 20px horizontally, 12px vertically, so the
    # box becomes (0,8)-(140,92): clamped at x=0, not negative.
    cropped = _crop_via_detect(
        [_box(0.1, 0.2, 0.2, 0.3), _box(0.5, 0.6, 0.1, 0.2)], crop_margin=0.2
    )
    assert _size(cropped) == (140, 84)


def test_crop_clamps_to_the_frame():
    """A box running off the right edge must not produce a crop wider than the frame."""
    cropped = _crop_via_detect([_box(0.9, 0.9, 0.2, 0.2)], crop_margin=0.5)
    w, h = _size(cropped)
    assert w <= 200 and h <= 100


def test_crop_is_scale_free_so_it_survives_an_roi_crop():
    """Normalized boxes mean the same detections crop proportionally on any size.

    onnx_v1 may letterbox an ROI-cropped region but always reports full-frame
    normalized coords, so _crop must not assume any particular pixel size.
    """
    # Both sizes chosen so MIN_CROP_PX does not engage: at 200x100 the crop height
    # would be 50 and get widened to 64, which is right but is not the proportionality
    # this test is checking.
    boxes = [_box(0.25, 0.25, 0.5, 0.5)]
    small = _size(_crop_via_detect(boxes, image=_jpeg(400, 200)))
    large = _size(_crop_via_detect(boxes, image=_jpeg(800, 400)))
    assert small == (200, 100)
    assert large == (400, 200)


def test_a_thin_detection_is_widened_to_a_usable_crop():
    """A crop smaller than a vision encoder's patch factor CRASHES the model.

    Measured against qwen2.5vl:3b over the 340 labelled frames: 88 calls -- every one
    of them a cropped frame -- killed the model runner with
    "height:12 or width:38 must be larger than factor:28". The old guard only rejected
    a zero-or-negative area, so a detector box a dozen pixels tall went straight
    through. The detector produces such boxes routinely on a 480x640 frame.
    """
    # A 480x640 frame with a 2%-tall, 8%-wide box: 38x12 px, exactly the shape that
    # panicked the runner.
    cropped = _crop_via_detect([_box(0.4, 0.5, 0.08, 0.02)], image=_jpeg(480, 640))
    w, h = _size(cropped)
    assert w >= MIN_CROP_PX and h >= MIN_CROP_PX, f"crop {w}x{h} is below the floor"


def test_widening_keeps_the_crop_inside_the_frame():
    """Growing a crop at the edge must move the window, not overflow the image."""
    for x, y in ((0.0, 0.0), (0.99, 0.99)):
        cropped = _crop_via_detect([_box(x, y, 0.01, 0.01)], image=_jpeg(480, 640))
        w, h = _size(cropped)
        assert MIN_CROP_PX <= w <= 480
        assert MIN_CROP_PX <= h <= 640


def test_widening_leaves_an_already_large_crop_alone():
    """The floor is a floor, not a resize: a big crop must not be touched."""
    cropped = _crop_via_detect([_box(0.25, 0.25, 0.5, 0.5)], image=_jpeg(480, 640))
    assert _size(cropped) == (240, 320)


def test_an_image_smaller_than_the_floor_falls_back_to_the_full_frame():
    """No frame in this corpus is this small, but the crop must not invent pixels."""
    image = _jpeg(40, 40)
    assert _crop_via_detect([_box(0.4, 0.4, 0.1, 0.1)], image=image) == image


def test_no_boxes_sends_the_full_frame():
    image = _jpeg()
    assert _crop_via_detect([], image=image) == image


def test_entries_without_a_bbox_are_skipped():
    """A _vlm_verdict entry rides in the same list and has no bbox — it must not crash."""
    image = _jpeg()
    detections = [{"_vlm_verdict": {"is_pothole": True}}, "not-a-dict"]
    assert _crop_via_detect(detections, image=image) == image


def test_a_degenerate_box_sends_the_full_frame():
    """Zero-area box → the crop rectangle collapses; sending the whole frame is valid."""
    image = _jpeg()
    assert _crop_via_detect([_box(0.5, 0.5, 0.0, 0.0)], image=image) == image
