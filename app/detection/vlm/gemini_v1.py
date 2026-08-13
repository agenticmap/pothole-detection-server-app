"""Gemini (Google) vision verifier — cheapest/fastest cloud option.

Sends the frame bytes + the shared verify prompt and parses the strict-JSON reply.
The `google-genai` SDK is imported lazily so the base server doesn't require it
unless vlm_backend='gemini'. Default model is gemini-2.5-flash (low cost/latency).
"""

from __future__ import annotations

import logging

from app.detection.vlm.base import VERIFY_PROMPT, VlmVerdict, parse_verdict

logger = logging.getLogger(__name__)

VERSION = "vlm.gemini_v1"
DEFAULT_MODEL = "gemini-2.5-flash"


class GeminiVerifier:
    version = VERSION

    def __init__(self, *, api_key: str, model_id: str = "", timeout: float = 30.0):
        from google import genai  # lazy — only needed for this backend

        self.model_id = model_id or DEFAULT_MODEL
        self._genai = genai
        # Client() also reads GEMINI_API_KEY/GOOGLE_API_KEY from the env when empty.
        self._client = genai.Client(api_key=api_key) if api_key else genai.Client()

    def verify(self, image: bytes, context: dict) -> VlmVerdict:
        from google.genai import types

        resp = self._client.models.generate_content(
            model=self.model_id,
            contents=[
                types.Part.from_bytes(data=image, mime_type="image/jpeg"),
                VERIFY_PROMPT,
            ],
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return parse_verdict(resp.text or "", self.model_id)
