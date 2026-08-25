"""Hybrid detector — YOLO Stage 1 + VLM verifier on the gray zone.

Implements the FrameDetector protocol by composing a Stage-1 detector (onnx/http)
with a VlmVerifier, so the worker, fusion, and DB schema are untouched — it's just
another detector backend.

Per frame:
  1. Run Stage 1 → probability p1 + detections.
  2. If p1 is decisive (outside [low, high]) or there's no verifier → return Stage 1.
  3. Otherwise (gray zone): crop to the detections, ask the VLM, and blend its
     verdict into p1 in logit space (VLM dominates). The verdict (incl. severity +
     rationale) rides along in `detections` so it lands in server_detections JSONB.

A per-run call cap (vlm_max_calls_per_run) bounds VLM cost. Because the worker
builds a fresh detector each run (get_detector()), the instance counter resets
per run. Frames skipped due to the cap fall back to Stage 1 (logged, not silent).
"""

from __future__ import annotations

import io
import logging

from app.detection.engine import DetectionResult, FrameDetector
from app.detection.vlm.base import VlmVerdict, VlmVerifier
from app.fusion.engine import clamp01, logit, sigmoid

logger = logging.getLogger(__name__)

VERSION = "detection.hybrid_v1"


class HybridDetector:
    version = VERSION

    def __init__(
        self,
        *,
        stage1: FrameDetector,
        verifier: VlmVerifier | None,
        low: float = 0.40,
        high: float = 0.75,
        blend_weight: float = 0.7,
        crop: bool = True,
        crop_margin: float = 0.20,
        max_calls: int = 50,
    ):
        self.stage1 = stage1
        self.verifier = verifier
        self.low = low
        self.high = high
        self.blend_weight = clamp01(blend_weight)
        self.crop = crop
        self.crop_margin = crop_margin
        self.max_calls = max_calls
        self._calls = 0          # VLM calls made this run (instance == one run)
        self._cap_logged = False  # log the cap-hit once per run, not per frame

    def detect(self, jpeg: bytes) -> DetectionResult:
        r1 = self.stage1.detect(jpeg)
        p1 = r1.probability

        # Decisive Stage-1 result, no verifier, or gray-zone gate not met → trust Stage 1.
        if self.verifier is None or p1 is None or not (self.low <= p1 <= self.high):
            return r1

        # Cost cap: beyond the per-run budget, gray-zone frames fall back to Stage 1.
        if self._calls >= self.max_calls:
            if not self._cap_logged:
                logger.warning(
                    "VLM call cap (%d) reached this run; further gray-zone frames "
                    "fall back to Stage-1-only.",
                    self.max_calls,
                )
                self._cap_logged = True
            return r1

        image = self._crop(jpeg, r1.detections) if self.crop else jpeg
        self._calls += 1
        try:
            verdict = self.verifier.verify(image, {"stage1_p": p1})
        except Exception as e:  # noqa: BLE001 — a VLM failure must not wedge the frame
            logger.warning("VLM verify failed (%s); falling back to Stage-1 probability.", e)
            return r1

        p_final = self._blend(p1, verdict)
        model_id = f"{r1.model_id}+{verdict.model_id}" if verdict.model_id else r1.model_id
        detections = list(r1.detections) + [
            {
                "_vlm_verdict": {
                    "is_pothole": verdict.is_pothole,
                    "confidence": verdict.confidence,
                    "severity": verdict.severity,
                    "rationale": verdict.rationale,
                    "model_id": verdict.model_id,
                }
            }
        ]
        return DetectionResult(
            probability=p_final, detections=detections, model_id=model_id, version=VERSION
        )

    def _blend(self, p1: float, verdict: VlmVerdict) -> float:
        """Logit-space blend; the VLM verdict dominates on gray-zone frames.

        The verdict becomes a pothole-probability (confidence if is_pothole, else
        1 - confidence), then combines with p1 weighted by blend_weight — consistent
        with the project's logit-space fusion (app/fusion/matlab_port_v1.py).
        """
        p_vlm = verdict.confidence if verdict.is_pothole else (1.0 - verdict.confidence)
        w = self.blend_weight
        return sigmoid((1.0 - w) * logit(clamp01(p1)) + w * logit(clamp01(p_vlm)))

    def _crop(self, jpeg: bytes, detections: list[dict]) -> bytes:
        """Crop to the union of detection bboxes + margin, re-encoded as JPEG.

        Detections are {"bbox": {"x","y","w","h"}} — normalized [0,1] corner-origin,
        the same shape the device sends (see onnx_v1). Normalized rather than pixel
        coords means this works unchanged whether or not Stage 1 applied an ROI crop.
        Returns the original bytes if there are no usable boxes or Pillow isn't
        available — the VLM then sees the whole frame, which is still valid.
        """
        boxes = [
            d["bbox"]
            for d in detections
            if isinstance(d, dict) and isinstance(d.get("bbox"), dict)
        ]
        if not boxes:
            return jpeg
        try:
            from PIL import Image  # lazy — only when cropping is enabled

            img = Image.open(io.BytesIO(jpeg)).convert("RGB")
            w, h = img.size
            xs1 = [b["x"] * w for b in boxes]
            ys1 = [b["y"] * h for b in boxes]
            xs2 = [(b["x"] + b["w"]) * w for b in boxes]
            ys2 = [(b["y"] + b["h"]) * h for b in boxes]
            x1, y1, x2, y2 = min(xs1), min(ys1), max(xs2), max(ys2)
            mx, my = (x2 - x1) * self.crop_margin, (y2 - y1) * self.crop_margin
            box = (
                int(max(0, x1 - mx)),
                int(max(0, y1 - my)),
                int(min(w, x2 + mx)),
                int(min(h, y2 + my)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                return jpeg
            buf = io.BytesIO()
            img.crop(box).save(buf, format="JPEG")
            return buf.getvalue()
        except Exception as e:  # noqa: BLE001 — crop is best-effort; fall back to full frame
            logger.debug("Crop failed (%s); sending full frame to VLM.", e)
            return jpeg
