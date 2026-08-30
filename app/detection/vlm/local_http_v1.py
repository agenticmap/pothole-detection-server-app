"""VLM verifier over an OpenAI-compatible chat endpoint — local or cloud.

One client covers every provider that speaks POST /v1/chat/completions with vision
content blocks, which is most of them:

    ollama      http://localhost:11434/v1/chat/completions   (no key, nothing leaves the host)
    openrouter  https://openrouter.ai/api/v1/chat/completions (Bearer key, any hosted VLM)
    local_http  vLLM / LM Studio / anything else              (VLM_HTTP_URL required)

Uses stdlib urllib (no new dependency), mirroring app/detection/http_v1.py.
app/detection/vlm/registry.py supplies the default URL per backend; VLM_HTTP_URL
overrides it. Run any open VLM (Qwen2.5-VL, LLaVA, ...) behind the local path.
"""

from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request

from app.detection.vlm.base import VERIFY_PROMPT, VlmVerdict, parse_verdict

logger = logging.getLogger(__name__)

VERSION = "vlm.local_http_v1"
DEFAULT_MODEL = "qwen2.5-vl"


class LocalHttpVerifier:
    version = VERSION

    def __init__(
        self,
        *,
        url: str,
        model_id: str = "",
        api_key: str = "",
        timeout: float = 30.0,
        json_mode: bool = True,
        extra_headers: dict[str, str] | None = None,
    ):
        if not url:
            raise ValueError("vlm_http_url is required for this VLM backend")
        self.url = url
        self.model_id = model_id or DEFAULT_MODEL
        self.api_key = api_key
        self.timeout = timeout
        self.json_mode = json_mode
        self.extra_headers = dict(extra_headers or {})

    def verify(self, image: bytes, context: dict) -> VlmVerdict:
        b64 = base64.standard_b64encode(image).decode("utf-8")
        payload_body: dict = {
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
        # Ollama's OpenAI shim and most OpenRouter vision models honour this and stop
        # wrapping the verdict in prose or fences. Opt-out because some models 400 on
        # the field; parse_verdict's regex extraction is the fallback either way.
        if self.json_mode:
            payload_body["response_format"] = {"type": "json_object"}
        body = json.dumps(payload_body).encode("utf-8")

        headers = {"Content-Type": "application/json", **self.extra_headers}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:  # noqa: S310 — configured URL
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # The status alone ("HTTP Error 400: Bad Request") never says WHICH of the
            # likely causes fired -- wrong key, unknown model id, a text-only model, no
            # credit, response_format unsupported. The body says exactly which, and the
            # hybrid detector only logs str(e), so fold it in or it is lost.
            raise RuntimeError(f"{e} from {self.url} ({self.model_id}): {_body(e)}") from e

        text = payload["choices"][0]["message"]["content"]
        return parse_verdict(text, self.model_id)


def _body(e: urllib.error.HTTPError, limit: int = 500) -> str:
    """The error response body, truncated. Never raises -- it is used inside except."""
    try:
        return e.read().decode("utf-8", errors="replace")[:limit]
    except Exception:  # noqa: BLE001 — a body we cannot read must not mask the HTTPError
        return "<no body>"
