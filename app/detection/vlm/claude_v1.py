"""Claude (Anthropic) vision verifier.

Sends the frame as a base64 image block plus the shared verify prompt, and uses
structured outputs (output_config.format) to force the strict-JSON verdict shape.
The `anthropic` SDK is imported lazily so the base server doesn't require it unless
vlm_backend='claude'.

Model default is claude-opus-4-8. For a high-volume crowd app, set VLM_MODEL_ID to a
cheaper vision tier (claude-sonnet-4-6 or claude-haiku-4-5) — see .env.example.
"""

from __future__ import annotations

import base64
import logging

from app.detection.vlm.base import VERDICT_SCHEMA, VERIFY_PROMPT, VlmVerdict, parse_verdict

logger = logging.getLogger(__name__)

VERSION = "vlm.claude_v1"
DEFAULT_MODEL = "claude-opus-4-8"


class ClaudeVerifier:
    version = VERSION

    def __init__(self, *, api_key: str, model_id: str = "", timeout: float = 30.0):
        import anthropic  # lazy — only needed for this backend

        self.model_id = model_id or DEFAULT_MODEL
        # Anthropic() also reads ANTHROPIC_API_KEY from the env when api_key is empty.
        self._client = (
            anthropic.Anthropic(api_key=api_key, timeout=timeout)
            if api_key
            else anthropic.Anthropic(timeout=timeout)
        )

    def verify(self, image: bytes, context: dict) -> VlmVerdict:
        b64 = base64.standard_b64encode(image).decode("utf-8")
        resp = self._client.messages.create(
            model=self.model_id,
            max_tokens=512,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": b64,
                            },
                        },
                        {"type": "text", "text": VERIFY_PROMPT},
                    ],
                }
            ],
            output_config={"format": {"type": "json_schema", "schema": VERDICT_SCHEMA}},
        )
        text = next((b.text for b in resp.content if b.type == "text"), "")
        return parse_verdict(text, self.model_id)
