"""Detector selection — returns the configured backend, or None when disabled.

Mirrors app/fusion/registry.py::get_engine. Backend modules are imported lazily so
the base server doesn't hard-require onnxruntime/Pillow unless backend='onnx'.
"""

from __future__ import annotations

from app.config import settings
from app.detection.engine import FrameDetector
from app.detection.vlm.registry import VlmProfile


def _build_detector(backend: str) -> FrameDetector | None:
    """Construct a leaf (non-hybrid) detector backend, or None for 'none'."""
    if backend == "onnx":
        from app.detection.onnx_v1 import OnnxYoloDetector

        return OnnxYoloDetector(
            model_path=settings.detection_model_path,
            model_id=settings.detection_model_id,
            input_size=settings.detection_input_size,
            conf_threshold=settings.detection_conf_threshold,
            iou_threshold=settings.detection_iou_threshold,
            roi_enabled=settings.detection_roi_enabled,
            roi_top=settings.detection_roi_top,
            roi_bottom=settings.detection_roi_bottom,
            labels=settings.detection_class_name_list,
            primary_class_id=settings.detection_primary_class_id,
        )
    if backend == "http":
        from app.detection.http_v1 import HttpDetector

        return HttpDetector(
            url=settings.detection_http_url,
            model_id=settings.detection_model_id,
            timeout=settings.detection_http_timeout,
        )
    return None


def get_detector(vlm_profile: VlmProfile | None = None) -> FrameDetector | None:
    """Pick the detector for settings.detection_backend, or None for 'none'.

    `vlm_profile` is threaded to the hybrid backend's verifier so a caller can
    choose a VLM per request instead of mutating the process-wide settings
    singleton -- see app/detection/vlm/registry.py::VlmProfile for why that
    distinction matters. Additive: the default is today's behaviour exactly.
    """
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
            verifier=get_verifier(vlm_profile),
            low=settings.vlm_verify_low,
            high=settings.vlm_verify_high,
            blend_weight=settings.vlm_blend_weight,
            crop=settings.vlm_crop_to_detections,
            crop_margin=settings.vlm_crop_margin,
            max_calls=settings.vlm_max_calls_per_run,
        )
    return _build_detector(backend)


__all__ = ["get_detector"]
