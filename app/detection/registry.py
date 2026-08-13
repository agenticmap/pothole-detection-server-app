"""Detector selection — returns the configured backend, or None when disabled.

Mirrors app/fusion/registry.py::get_engine. Backend modules are imported lazily so
the base server doesn't hard-require onnxruntime/Pillow unless backend='onnx'.
"""

from __future__ import annotations

from app.config import settings
from app.detection.engine import FrameDetector


def _build_detector(backend: str) -> FrameDetector | None:
    """Construct a leaf (non-hybrid) detector backend, or None for 'none'."""
    if backend == "onnx":
        from app.detection.onnx_v1 import OnnxYoloDetector

        return OnnxYoloDetector(
            model_path=settings.detection_model_path,
            model_id=settings.detection_model_id,
            input_size=settings.detection_input_size,
            conf_threshold=settings.detection_conf_threshold,
        )
    if backend == "http":
        from app.detection.http_v1 import HttpDetector

        return HttpDetector(
            url=settings.detection_http_url,
            model_id=settings.detection_model_id,
        )
    return None


def get_detector() -> FrameDetector | None:
    """Pick the detector for settings.detection_backend, or None for 'none'."""
    backend = settings.detection_backend
    if backend == "hybrid":
        from app.detection.hybrid_v1 import HybridDetector
        from app.detection.vlm.registry import get_verifier

        stage1 = _build_detector(settings.detection_hybrid_stage1)
        if stage1 is None:
            raise ValueError(
                "detection_hybrid_stage1 must be 'onnx' or 'http' for backend='hybrid'"
            )
        return HybridDetector(
            stage1=stage1,
            verifier=get_verifier(),
            low=settings.vlm_verify_low,
            high=settings.vlm_verify_high,
            blend_weight=settings.vlm_blend_weight,
            crop=settings.vlm_crop_to_detections,
            crop_margin=settings.vlm_crop_margin,
            max_calls=settings.vlm_max_calls_per_run,
        )
    return _build_detector(backend)


__all__ = ["get_detector"]
