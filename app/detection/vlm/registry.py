"""Verifier selection — returns the configured VLM backend, or None when disabled.

Mirrors app/detection/registry.py::get_detector. Backend modules import their SDK
(anthropic / google-genai) or use stdlib HTTP lazily, so the base server doesn't
hard-require a cloud SDK unless that backend is selected.

"openrouter" and "ollama" are not separate clients -- both speak the same
OpenAI-compatible wire format as "local_http", so all three share LocalHttpVerifier
and differ only in the default URL and whether a key is required. They exist as
names because nobody would guess that VLM_BACKEND=local_http is how you reach
OpenRouter, and because a default URL is the difference between one env var and
three. VLM_HTTP_URL still overrides the default for any of them.
"""

from __future__ import annotations

from app.config import settings
from app.detection.vlm.base import VlmVerifier

# Backends served by LocalHttpVerifier: {name: (default url, api key required)}.
OPENAI_COMPATIBLE: dict[str, tuple[str, bool]] = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", True),
    "ollama": ("http://localhost:11434/v1/chat/completions", False),
    "local_http": ("", False),
}


def get_verifier() -> VlmVerifier | None:
    """Pick the verifier for settings.vlm_backend, or None for 'none'."""
    backend = settings.vlm_backend
    if backend in OPENAI_COMPATIBLE:
        from app.detection.vlm.local_http_v1 import LocalHttpVerifier

        default_url, key_required = OPENAI_COMPATIBLE[backend]
        if key_required and not settings.vlm_api_key:
            raise ValueError(f"VLM_API_KEY is required for vlm_backend='{backend}'")
        # OpenRouter asks callers to identify themselves; both headers are optional
        # and only meaningful there, so they are not sent to a local server.
        extra: dict[str, str] = {}
        if backend == "openrouter":
            if settings.vlm_http_referer:
                extra["HTTP-Referer"] = settings.vlm_http_referer
            if settings.vlm_http_title:
                extra["X-Title"] = settings.vlm_http_title
        return LocalHttpVerifier(
            url=settings.vlm_http_url or default_url,
            model_id=settings.vlm_model_id,
            api_key=settings.vlm_api_key,
            timeout=settings.vlm_timeout,
            json_mode=settings.vlm_json_mode,
            extra_headers=extra,
        )
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
    return None


__all__ = ["OPENAI_COMPATIBLE", "get_verifier"]
