"""In-process YOLO ONNX detector (default backend, CPU).

onnxruntime + Pillow are imported lazily (only when this backend is constructed),
so the base server doesn't require them unless detection_backend='onnx'.

Expects an Ultralytics ONNX export with **raw** output [1, 4+nc, N] (post-sigmoid
class scores, no NMS baked in) — see docs/reference/model-attribution.md for the pinned
export line. probability = the best PRIMARY-class box after conf-threshold + NMS.

Multi-class (Phase 2.7b). `labels` names each class by position and
`primary_class_id` picks the one that may set `server_probability`. Fusion blends
that scalar with no notion of class, so a confident manhole must not reach it —
see `_frame_probability`. A labels/nc mismatch is rejected at construction rather
than mislabelling every box.

Two Phase 2.7 additions, both driven by the real collected frames:

- **ROI crop.** Uploads are 480x640 *portrait* windshield shots: the top half is
  sky and trees and the hood takes the bottom ~15%, so letterboxing the whole
  frame spends most of the 640px budget on things that are never a pothole.
  Cropping to the road band first puts ~1.8x more pixels on road surface. Boxes
  are always returned in FULL-frame coordinates, so nothing downstream needs to
  know whether the crop was on.
- **Detection shape matches the device's.** server_detections and
  device_detections are the same column family and used to disagree on keys,
  origin and units. Both now emit
  {"bbox": {x, y, w, h} normalized corner-origin, "label", "class_id",
  "confidence"}.
"""

from __future__ import annotations

import io
import logging
from collections.abc import Sequence

import numpy as np

from app.detection.engine import DetectionResult

logger = logging.getLogger(__name__)

VERSION = "detection.onnx_yolo_v2"


class OnnxYoloDetector:
    version = VERSION

    def __init__(
        self,
        *,
        model_path: str,
        model_id: str,
        input_size: int = 640,
        conf_threshold: float = 0.25,
        iou_threshold: float = 0.45,
        roi_enabled: bool = False,
        roi_top: float = 0.0,
        roi_bottom: float = 1.0,
        labels: Sequence[str] = ("pothole",),
        primary_class_id: int = 0,
    ):
        import onnxruntime as ort  # lazy — only needed for this backend

        if not model_path:
            raise ValueError("detection_model_path is required for backend='onnx'")
        if not 0.0 <= roi_top < roi_bottom <= 1.0:
            raise ValueError(
                f"detection_roi_top/bottom must satisfy 0 <= top < bottom <= 1 "
                f"(got {roi_top}, {roi_bottom})"
            )
        labels = tuple(labels)
        if not labels:
            raise ValueError("labels must name at least one class")
        if not 0 <= primary_class_id < len(labels):
            raise ValueError(
                f"detection_primary_class_id {primary_class_id} is not a valid index into "
                f"{len(labels)} class name(s) {labels}"
            )
        self.model_id = model_id
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self.roi_enabled = roi_enabled
        self.roi_top = roi_top
        self.roi_bottom = roi_bottom
        self.labels = labels
        self.primary_class_id = primary_class_id
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._check_class_count()

    def detect(self, jpeg: bytes) -> DetectionResult:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        full = np.asarray(img)  # H, W, 3
        frame_h, frame_w = full.shape[:2]

        roi, offset_y = self._roi(full)
        tensor, ratio, (dw, dh) = self._letterbox(roi)
        output = self._session.run(None, {self._input_name: tensor})[0]
        boxes, scores, class_ids = self._postprocess(output)
        prob = self._frame_probability(scores, class_ids)

        detections = [
            self._to_detection(box, score, cls, ratio, dw, dh, offset_y, frame_w, frame_h)
            for box, score, cls in zip(boxes, scores, class_ids)
        ]
        return DetectionResult(
            probability=prob, detections=detections, model_id=self.model_id, version=VERSION
        )

    def _frame_probability(self, scores: np.ndarray, class_ids: np.ndarray) -> float:
        """The best PRIMARY-class box — not simply the best box.

        Fusion blends this scalar with the sensor verdict and has no notion of class
        (`app/fusion/service.py` `_CANDIDATE_COLUMNS`), so a confident manhole scored
        as the frame probability would be read as a confirmed *pothole*. Taking the
        max across all classes was correct only while nc == 1.

        A frame holding only non-primary boxes scores 0.0, which
        `NULLIF(server_probability, 0.0)` already reads as *no measurement* → fall
        back to `device_probability`. That is the intended semantics rather than a
        gap: "that is a manhole" is not evidence about a pothole in either
        direction, so it should leave the sensor verdict alone.
        """
        if scores.size == 0:
            return 0.0
        primary = scores[class_ids == self.primary_class_id]
        return float(primary.max()) if primary.size else 0.0

    def _label_for(self, class_id: int) -> str:
        """Class id → name. Out-of-range is unreachable once `_check_layout` passes."""
        if 0 <= class_id < len(self.labels):
            return self.labels[class_id]
        return f"class_{class_id}"

    def _check_class_count(self) -> None:
        """Reject a labels/nc mismatch at construction, when the export declares its shape.

        A 4-class model loaded with one class name mislabels every box and, worse,
        scores the wrong class into `server_probability`. `_check_layout` catches it
        too, but only on the first frame — and `app/detection/service.py` swallows
        per-frame exceptions into a NULL, so the mismatch would present as
        "detection silently does nothing, forever". Failing here fails at boot.

        Ultralytics exports non-dynamic by default, so the channel axis is usually a
        concrete int. When it is not — or when the shape is not a plausible raw
        `[1, 4+nc, N]` layout — say nothing and leave it to `_check_layout`, which
        has the actual array.
        """
        get_outputs = getattr(self._session, "get_outputs", None)
        if get_outputs is None:
            return
        try:
            shape = list(get_outputs()[0].shape)
        except Exception:  # noqa: BLE001 — shape metadata is optional, not load-bearing
            return
        if len(shape) != 3:
            return
        channels, anchors = shape[1], shape[2]
        if not isinstance(channels, int) or not isinstance(anchors, int):
            return
        if channels < 5 or channels > anchors:
            return  # not a raw layout at all — _check_layout will say so, with better wording
        self._assert_class_count(channels)

    def _assert_class_count(self, channels: int) -> None:
        if channels - 4 != len(self.labels):
            raise ValueError(
                f"Model has {channels - 4} class(es) but {len(self.labels)} class name(s) "
                f"were configured {self.labels}. Every box would be mislabelled and the "
                f"frame probability could come from the wrong class. Set "
                f"DETECTION_CLASS_NAMES to the model's data.yaml `names:`, in order."
            )

    # ── Geometry ──────────────────────────────────────────────────────────────

    def _roi(self, img: np.ndarray) -> tuple[np.ndarray, int]:
        """Crop to the road band. Returns (cropped view, y offset in full-frame px).

        Full width is kept — potholes appear across the whole road and the phone's
        yaw is not controlled. Only the vertical extent is trimmed, which is what
        the sky/hood problem actually is.
        """
        if not self.roi_enabled:
            return img, 0
        h = img.shape[0]
        y0 = int(round(h * self.roi_top))
        y1 = int(round(h * self.roi_bottom))
        if y1 - y0 < 2:  # degenerate crop — fall back rather than feed a 0-row tensor
            logger.warning(
                "ROI crop produced %d rows on a %d-row frame; using the full frame.", y1 - y0, h
            )
            return img, 0
        return img[y0:y1], y0

    def _letterbox(self, img: np.ndarray):
        """Resize keeping aspect ratio + pad to a square. Returns (NCHW tensor, ratio, (dw, dh))."""
        h, w = img.shape[:2]
        size = self.input_size
        ratio = min(size / h, size / w)
        nh, nw = int(round(h * ratio)), int(round(w * ratio))

        from PIL import Image

        resized = np.asarray(Image.fromarray(img).resize((nw, nh), Image.BILINEAR))
        canvas = np.full((size, size, 3), 114, dtype=np.uint8)
        dw, dh = (size - nw) // 2, (size - nh) // 2
        canvas[dh : dh + nh, dw : dw + nw] = resized

        tensor = canvas.astype(np.float32) / 255.0
        tensor = np.transpose(tensor, (2, 0, 1))[np.newaxis, ...]  # 1,3,H,W
        return np.ascontiguousarray(tensor), ratio, (dw, dh)

    def _to_detection(
        self,
        box: np.ndarray,
        score: float,
        class_id: int,
        ratio: float,
        dw: int,
        dh: int,
        offset_y: int,
        frame_w: int,
        frame_h: int,
    ) -> dict:
        """Letterboxed centre-form box → full-frame normalized corner-form dict.

        Three coordinate hops, in order: undo the letterbox pad and scale, add the
        ROI offset back so the box is in *full*-frame pixels, then normalize.
        Forgetting the ROI hop is the bug this long signature exists to prevent.
        """
        cx, cy, w, h = (float(v) for v in box)
        cx = (cx - dw) / ratio
        cy = (cy - dh) / ratio + offset_y
        w = w / ratio
        h = h / ratio

        # Clamp to the frame, then derive extent from the clamped corners so that
        # x + w and y + h stay inside it.
        x1 = min(max(cx - w / 2, 0.0), float(frame_w))
        y1 = min(max(cy - h / 2, 0.0), float(frame_h))
        x2 = min(max(cx + w / 2, 0.0), float(frame_w))
        y2 = min(max(cy + h / 2, 0.0), float(frame_h))

        return {
            "bbox": {
                "x": x1 / frame_w,
                "y": y1 / frame_h,
                "w": (x2 - x1) / frame_w,
                "h": (y2 - y1) / frame_h,
            },
            "label": self._label_for(int(class_id)),
            "class_id": int(class_id),
            "confidence": float(score),
        }

    # ── Decode ────────────────────────────────────────────────────────────────

    def _postprocess(self, output: np.ndarray):
        """Decode [1, 4+nc, N] → (boxes xywh, scores, class_ids) after conf-threshold + NMS."""
        self._check_layout(output)
        pred = np.squeeze(output, axis=0).T  # N, 4+nc
        boxes = pred[:, :4]
        cls_scores = pred[:, 4:]
        scores = cls_scores.max(axis=1)
        class_ids = cls_scores.argmax(axis=1)
        keep = scores >= self.conf_threshold
        boxes, scores, class_ids = boxes[keep], scores[keep], class_ids[keep]
        if scores.size == 0:
            return np.empty((0, 4)), np.empty((0,)), np.empty((0,), dtype=int)
        idx = self._nms(boxes, scores)
        return boxes[idx], scores[idx], class_ids[idx]

    def _check_layout(self, output: np.ndarray) -> None:
        """Fail loudly on an export this decoder cannot read.

        A wrong layout decodes into plausible-looking nonsense rather than raising,
        which is the worst failure mode for a scoring pipeline. Raw Ultralytics
        output is [1, 4+nc, N] with N in the thousands, so the channel axis is the
        *small* one. Both [1, 300, 6] (exported with nms=True) and [1, N, 4+nc]
        (transposed) trip this.
        """
        if output.ndim != 3 or output.shape[0] != 1:
            raise ValueError(
                f"Unexpected detector output shape {output.shape}; expected [1, 4+nc, N]."
            )
        channels, anchors = output.shape[1], output.shape[2]
        if channels < 5 or channels > anchors:
            raise ValueError(
                f"Detector output {output.shape} is not the raw Ultralytics [1, 4+nc, N] layout "
                f"this backend decodes. An export with NMS baked in ([1, 300, 6]) or a transposed "
                f"export ([1, N, 4+nc]) would silently decode to garbage. Re-export with: "
                f"yolo export model=best.pt format=onnx imgsz={self.input_size} opset=12 nms=False"
            )
        self._assert_class_count(channels)

    def _nms(self, boxes_xywh: np.ndarray, scores: np.ndarray) -> list[int]:
        """Greedy NMS on center-form boxes. Returns kept indices."""
        cx, cy, w, h = boxes_xywh.T
        x1, y1, x2, y2 = cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2
        areas = (x2 - x1) * (y2 - y1)
        order = scores.argsort()[::-1]
        keep: list[int] = []
        while order.size > 0:
            i = order[0]
            keep.append(int(i))
            xx1 = np.maximum(x1[i], x1[order[1:]])
            yy1 = np.maximum(y1[i], y1[order[1:]])
            xx2 = np.minimum(x2[i], x2[order[1:]])
            yy2 = np.minimum(y2[i], y2[order[1:]])
            inter = np.maximum(0, xx2 - xx1) * np.maximum(0, yy2 - yy1)
            iou = inter / (areas[i] + areas[order[1:]] - inter + 1e-9)
            order = order[1:][iou <= self.iou_threshold]
        return keep
