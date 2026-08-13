"""Unit tests for the hybrid detector (Stage-1 + VLM verifier) and verdict parsing.

Pure unit tests — no Postgres, no onnxruntime, no cloud SDK. A StubStage1Detector
and StubVlmVerifier exercise the gray-zone gate, the logit blend direction, the
per-run call cap, crop on/off, and graceful VLM failure. parse_verdict is tested
against fenced / prose-wrapped / malformed replies.
"""

import pytest

from app.detection.engine import DetectionResult
from app.detection.hybrid_v1 import HybridDetector
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
    det = _hybrid(StubStage1Detector(0.5, detections=[{"xywh": [1, 1, 1, 1]}]), v, crop=False)
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
