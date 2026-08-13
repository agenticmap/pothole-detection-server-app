"""External HTTP detector — offloads inference to a GPU service (Modal/Replicate/Triton).

POSTs the raw JPEG and expects JSON: {"probability": float, "detections": [...],
"model_id": str?}. Uses urllib (stdlib) to avoid a new dependency.
"""

from __future__ import annotations

import json
import logging
import urllib.request

from app.detection.engine import DetectionResult

logger = logging.getLogger(__name__)

VERSION = "detection.http_v1"


class HttpDetector:
    version = VERSION

    def __init__(self, *, url: str, model_id: str, timeout: float = 30.0):
        if not url:
            raise ValueError("detection_http_url is required for backend='http'")
        self.url = url
        self.model_id = model_id
        self.timeout = timeout

    def detect(self, jpeg: bytes) -> DetectionResult:
        req = urllib.request.Request(
            self.url, data=jpeg, headers={"Content-Type": "image/jpeg"}, method="POST"
        )
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — configured URL
            payload = json.loads(resp.read().decode("utf-8"))
        return DetectionResult(
            probability=payload.get("probability"),
            detections=payload.get("detections", []),
            model_id=payload.get("model_id", self.model_id),
            version=VERSION,
        )
