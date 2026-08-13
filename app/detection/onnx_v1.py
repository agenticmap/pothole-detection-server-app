"""In-process YOLOv8 ONNX detector (default backend, CPU).

onnxruntime + Pillow are imported lazily (only when this backend is constructed),
so the base server doesn't require them unless detection_backend='onnx'.

Expects an Ultralytics YOLOv8 ONNX export with output [1, 4+nc, N] (post-sigmoid
class scores). probability = max box confidence after conf-threshold + NMS.
"""

from __future__ import annotations

import io
import logging

import numpy as np

from app.detection.engine import DetectionResult

logger = logging.getLogger(__name__)

VERSION = "detection.onnx_yolov8_v1"


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
    ):
        import onnxruntime as ort  # lazy — only needed for this backend

        if not model_path:
            raise ValueError("detection_model_path is required for backend='onnx'")
        self.model_id = model_id
        self.input_size = input_size
        self.conf_threshold = conf_threshold
        self.iou_threshold = iou_threshold
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name

    def detect(self, jpeg: bytes) -> DetectionResult:
        from PIL import Image

        img = Image.open(io.BytesIO(jpeg)).convert("RGB")
        orig = np.asarray(img)  # H, W, 3
        tensor, ratio, (dw, dh) = self._letterbox(orig)
        output = self._session.run(None, {self._input_name: tensor})[0]
        boxes, scores = self._postprocess(output)
        prob = float(scores.max()) if scores.size else 0.0

        detections: list[dict] = []
        for box, score in zip(boxes, scores):
            # Map letterboxed-input xywh back to original-image pixel coords.
            cx, cy, w, h = box
            cx = (cx - dw) / ratio
            cy = (cy - dh) / ratio
            w = w / ratio
            h = h / ratio
            xywh = [float(cx), float(cy), float(w), float(h)]
            detections.append({"conf": float(score), "xywh": xywh})

        return DetectionResult(
            probability=prob, detections=detections, model_id=self.model_id, version=VERSION
        )

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

    def _postprocess(self, output: np.ndarray):
        """Decode [1, 4+nc, N] → (boxes xywh, scores) after conf-threshold + NMS."""
        pred = np.squeeze(output, axis=0).T  # N, 4+nc
        boxes = pred[:, :4]
        scores = pred[:, 4:].max(axis=1)
        keep = scores >= self.conf_threshold
        boxes, scores = boxes[keep], scores[keep]
        if scores.size == 0:
            return np.empty((0, 4)), np.empty((0,))
        idx = self._nms(boxes, scores)
        return boxes[idx], scores[idx]

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
