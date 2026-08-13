"""Verifier contract — the seam every VLM backend (Claude, Gemini, local) implements.

Mirrors app/detection/engine.py's FrameDetector: a frozen result dataclass + a
structural Protocol, so backends swap by config and the hybrid detector stays
backend-agnostic + testable. A shared prompt and a tolerant JSON parser live here
so every backend produces the same VlmVerdict from a model's text reply.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Protocol

# One instruction for every backend. Models must reply with STRICT JSON only.
# The false-positive classes here are exactly what crowd-sourced phone frames are
# full of and what a bare YOLO confuses for potholes.
VERIFY_PROMPT = (
    "You are verifying whether this road image contains a genuine POTHOLE — a "
    "bowl-shaped cavity or broken-out section of the pavement surface. "
    "Explicitly REJECT look-alikes: shadows, manhole/utility covers, wet patches "
    "or puddles, tar crack-sealant lines, painted road markings, gravel, and "
    "ordinary surface texture or staining. "
    'Reply with STRICT JSON only — no prose, no code fences — exactly this shape: '
    '{"is_pothole": <true|false>, "confidence": <number 0..1>, '
    '"severity": <"shallow"|"medium"|"deep"|"none">, '
    '"rationale": "<one short sentence>"}. '
    "confidence is how sure you are of the is_pothole value. "
    'Use severity "none" when is_pothole is false.'
)

# JSON schema for backends that support structured outputs (e.g. Claude). Kept to
# the subset structured outputs allows (enum + additionalProperties:false; no
# numeric range constraints — confidence is clamped in parse_verdict instead).
VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "is_pothole": {"type": "boolean"},
        "confidence": {"type": "number"},
        "severity": {"type": "string", "enum": ["shallow", "medium", "deep", "none"]},
        "rationale": {"type": "string"},
    },
    "required": ["is_pothole", "confidence", "severity", "rationale"],
    "additionalProperties": False,
}

_SEVERITIES = {"shallow", "medium", "deep"}


@dataclass(frozen=True)
class VlmVerdict:
    """One verifier's judgement on a single frame/crop.

    is_pothole:  the verdict.
    confidence:  how sure the model is of is_pothole, 0..1 (clamped).
    severity:    "shallow"|"medium"|"deep" when is_pothole, else None.
    rationale:   one-sentence human-readable explanation (stored for review).
    model_id:    provider model id, for the audit trail.
    """

    is_pothole: bool
    confidence: float
    severity: str | None
    rationale: str
    model_id: str = ""


class VlmVerifier(Protocol):
    """Confirms/rejects a candidate pothole from raw image bytes."""

    version: str

    def verify(self, image: bytes, context: dict) -> VlmVerdict: ...


def parse_verdict(text: str, model_id: str) -> VlmVerdict:
    """Parse a model's reply into a VlmVerdict, tolerant of fences/prose.

    Extracts the first {...} object so a backend that wraps JSON in ```json fences
    or adds a sentence still parses. Raises ValueError if no JSON object is present
    (the hybrid detector catches this and falls back to the Stage-1 probability).
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"no JSON object in VLM response: {text[:200]!r}")
    data = json.loads(match.group(0))

    is_pothole = bool(data.get("is_pothole", False))
    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))

    severity = data.get("severity")
    severity = severity if severity in _SEVERITIES else None
    if not is_pothole:
        severity = None  # severity is meaningless for a negative verdict

    rationale = str(data.get("rationale", ""))[:300]
    return VlmVerdict(
        is_pothole=is_pothole,
        confidence=confidence,
        severity=severity,
        rationale=rationale,
        model_id=model_id,
    )
