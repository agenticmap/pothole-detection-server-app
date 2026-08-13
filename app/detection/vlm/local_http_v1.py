"""Local / self-hosted VLM verifier over an OpenAI-compatible chat endpoint.

Covers vLLM, Ollama, LM Studio, or any server exposing POST /v1/chat/completions
with vision content blocks. Uses stdlib urllib (no new dependency), mirroring
app/detection/http_v1.py. Point VLM_HTTP_URL at the full chat-completions URL
(e.g. http://localhost:11434/v1/chat/completions) and run any open VLM
(Qwen2.5-VL, LLaVA, ...) behind it — this is the "plug in a local model" path.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.request

from app.detection.vlm.base import VERIFY_PROMPT, VlmVerdict, parse_verdict

logger = logging.getLogger(__name__)

VERSION = "vlm.local_http_v1"
DEFAULT_MODEL = "qwen2.5-vl"


class LocalHttpVerifier:
    version = VERSION

    def __init__(self, *, url: str, model_id: str = "", api_key: str = "", timeout: float = 30.0):
        if not url:
            raise ValueError("vlm_http_url is required for vlm_backend='local_http'")
        self.url = url
        self.model_id = model_id or DEFAULT_MODEL
        self.api_key = api_key
        self.timeout = timeout

    def verify(self, image: bytes, context: dict) -> VlmVerdict:
        b64 = base64.standard_b64encode(image).decode("utf-8")
        body = json.dumps(
            {
                "model": self.model_id,
                "max_tokens": 512,
                "temperature": 0,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": VERIFY_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                            },
                        ],
                    }
                ],
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — configured URL
            payload = json.loads(resp.read().decode("utf-8"))

        text = payload["choices"][0]["message"]["content"]
        return parse_verdict(text, self.model_id)
