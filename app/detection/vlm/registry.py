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

import os
from dataclasses import dataclass

from app.config import settings
from app.detection.vlm.base import VlmVerifier

# Backends served by LocalHttpVerifier: {name: (default url, api key required)}.
OPENAI_COMPATIBLE: dict[str, tuple[str, bool]] = {
    "openrouter": ("https://openrouter.ai/api/v1/chat/completions", True),
    "ollama": ("http://localhost:11434/v1/chat/completions", False),
    "local_http": ("", False),
}


@dataclass(frozen=True)
class VlmProfile:
    """One caller's choice of VLM, as a value rather than a global mutation.

    WHY THIS EXISTS. `get_verifier()` read `settings.vlm_backend` from the
    process-wide pydantic-settings singleton, so a per-request choice was not
    expressible at all. The only precedent for overriding it is
    `scripts/vlm_eval.py`, which assigns to `settings` directly -- safe there because
    a CLI is single-threaded and its own docstring says the flags must be "the single
    source of truth for what ran".

    **That pattern is actively dangerous in a request handler.** Under
    `uvicorn --workers 2` with concurrent requests, one request mutating
    `settings.vlm_backend` changes it for every other request in flight in that
    worker. `vlm_eval`'s warning -- "a run that silently used a different backend than
    the flag says would be worse than no measurement at all" -- becomes, on a server,
    *roadway imagery sent to a provider the operator did not choose*.
    `app/middleware/rate_limit.py` documents the same shape one level milder: module
    state under two workers gave each its own private ceiling.

    So a caller that wants a specific backend passes one of these. Nothing mutates.

    THE API KEY IS AN ENV VAR NAME, NEVER A VALUE. Stored and passed as
    `api_key_env`, resolved at construction. A profile can then be logged, echoed in a
    response, or dumped in a traceback without leaking a credential -- by
    construction, not by remembering to redact.
    """

    backend: str
    model_id: str = ""
    url: str = ""
    api_key_env: str = ""
    timeout: float = 30.0
    json_mode: bool = True

    def api_key(self) -> str:
        """Resolve the key from the environment. Empty when unset or unnamed."""
        return os.environ.get(self.api_key_env, "") if self.api_key_env else ""


def _profile_from_settings() -> VlmProfile:
    """Today's behaviour, as a profile. The default for every existing call site."""
    return VlmProfile(
        backend=settings.vlm_backend,
        model_id=settings.vlm_model_id,
        url=settings.vlm_http_url,
        timeout=settings.vlm_timeout,
        json_mode=settings.vlm_json_mode,
    )


def get_verifier(profile: VlmProfile | None = None) -> VlmVerifier | None:
    """Pick the verifier for `profile`, or for settings.vlm_backend when None.

    Additive: the default reproduces today's behaviour exactly, so no existing call
    site changes. See VlmProfile for why a parameter and not a mutation.
    """
    explicit = profile is not None
    p = profile or _profile_from_settings()
    # An explicit profile names its key by ENV VAR; the settings default keeps reading
    # settings.vlm_api_key, which is where every current deployment puts it.
    api_key = p.api_key() if explicit else settings.vlm_api_key
    backend = p.backend
    if backend in OPENAI_COMPATIBLE:
        from app.detection.vlm.local_http_v1 import LocalHttpVerifier

        default_url, key_required = OPENAI_COMPATIBLE[backend]
        if key_required and not api_key:
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
            url=p.url or default_url,
            model_id=p.model_id,
            api_key=api_key,
            timeout=p.timeout,
            json_mode=p.json_mode,
            extra_headers=extra,
        )
    if backend == "claude":
        from app.detection.vlm.claude_v1 import ClaudeVerifier

        return ClaudeVerifier(
            api_key=api_key,
            model_id=p.model_id,
            timeout=p.timeout,
        )
    if backend == "gemini":
        from app.detection.vlm.gemini_v1 import GeminiVerifier

        return GeminiVerifier(
            api_key=api_key,
            model_id=p.model_id,
            timeout=p.timeout,
        )
    return None


__all__ = ["OPENAI_COMPATIBLE", "get_verifier"]
