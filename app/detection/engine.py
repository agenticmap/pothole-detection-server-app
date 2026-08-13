"""Detector contract — the seam every backend (ONNX, HTTP, stub) implements.

Mirrors the fusion engine's Protocol + frozen-dataclass shape (app/fusion/engine.py)
so backends swap by config and the worker stays backend-agnostic + testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol


@dataclass(frozen=True)
class DetectionResult:
    """One detector's verdict on a single frame.

    probability: P(asset present), 0..1, or None if inference was inconclusive.
    detections:  list of boxes, e.g. {"conf": float, "xywh": [..]} — opaque to the worker.
    """

    probability: float | None
    detections: list[dict] = field(default_factory=list)
    model_id: str = ""
    version: str = ""


class FrameDetector(Protocol):
    """Runs inference on raw JPEG bytes."""

    version: str

    def detect(self, jpeg: bytes) -> DetectionResult: ...
