"""Verifier selection — returns the configured VLM backend, or None when disabled.

Mirrors app/detection/registry.py::get_detector. Backend modules import their SDK
(anthropic / google-genai) or use stdlib HTTP lazily, so the base server doesn't
hard-require a cloud SDK unless that backend is selected.
"""

from __future__ import annotations

from app.config import settings
from app.detection.vlm.base import VlmVerifier


def get_verifier() -> VlmVerifier | None:
    """Pick the verifier for settings.vlm_backend, or None for 'none'."""
    backend = settings.vlm_backend
    if backend == "claude":
        from app.detection.vlm.claude_v1 import ClaudeVerifier

        return ClaudeVerifier(
            api_key=settings.vlm_api_key,
            model_id=settings.vlm_model_id,
            timeout=settings.vlm_timeout,
        )
    if backend == "gemini":
        from app.detection.vlm.gemini_v1 import GeminiVerifier

        return GeminiVerifier(
            api_key=settings.vlm_api_key,
            model_id=settings.vlm_model_id,
            timeout=settings.vlm_timeout,
        )
    if backend == "local_http":
        from app.detection.vlm.local_http_v1 import LocalHttpVerifier

        return LocalHttpVerifier(
            url=settings.vlm_http_url,
            model_id=settings.vlm_model_id,
            api_key=settings.vlm_api_key,
            timeout=settings.vlm_timeout,
        )
    return None


__all__ = ["get_verifier"]
