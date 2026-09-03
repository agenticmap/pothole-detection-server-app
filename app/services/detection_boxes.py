"""Turn a `device_detections` / `server_detections` JSONB column into boxes.

Both columns hold the same shape by design -- `app/detection/onnx_v1.py::_to_detection`
deliberately emits what the Android client emits -- so one parser serves both:

    {"bbox": {"x":…, "y":…, "w":…, "h":…}, "label":…, "class_id":…, "confidence":…}

**Coordinates are normalized 0..1, corner-origin, and FULL-FRAME.** The ROI crop and
the letterbox padding are both already undone before storage (`_to_detection` adds
`offset_y` back), so a consumer needs no geometry knowledge -- multiply by the rendered
width and height and draw. This is the same convention `frame_box` stores human boxes
in; the centre-origin YOLO form exists only inside `scripts/export_labeled_frames.py`.
Introducing a second convention here would not crash, it would silently train on boxes
offset by half their size, which is the failure `tests/test_box_export.py` exists for.

The filter is **structural, not a special case on a key name**: an entry only survives
if it carries a dict `bbox`. That is what excludes the `{"_vlm_verdict": {...}}` element
`app/detection/hybrid_v1.py` appends to the same list -- and it will keep excluding
whatever non-box annotation gets appended next, without this file being edited.
"""

from __future__ import annotations

import json
from typing import Any


def _load(raw: Any) -> list:
    """asyncpg hands jsonb back as text; accept either form, guess at neither."""
    if not raw:
        return []
    try:
        items = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return []
    return items if isinstance(items, list) else []


def parse_detection_boxes(raw: Any) -> list[dict]:
    """Normalize a detections column into drawable boxes. Bad entries are dropped."""
    boxes: list[dict] = []
    for d in _load(raw):
        bbox = d.get("bbox") if isinstance(d, dict) else None
        if not isinstance(bbox, dict):
            continue
        try:
            box = {
                "x": float(bbox["x"]),
                "y": float(bbox["y"]),
                "w": float(bbox["w"]),
                "h": float(bbox["h"]),
                "confidence": float(d.get("confidence") or 0.0),
            }
        except (KeyError, TypeError, ValueError):
            continue
        label = d.get("label")
        if isinstance(label, str):
            box["label"] = label
        class_id = d.get("class_id")
        if isinstance(class_id, int) and not isinstance(class_id, bool):
            box["class_id"] = class_id
        boxes.append(box)
    return boxes


def parse_vlm_verdict(raw: Any) -> dict | None:
    """Pull the hybrid detector's VLM verdict out of the same list, if present.

    Surfaced rather than dropped because under `DETECTION_BACKEND=hybrid`
    `server_probability` is a *blend* (`hybrid_v1._blend`), and an operator looking at
    0.62 otherwise has no way to tell whether that is YOLO's opinion or a VLM override.
    The rationale is the only human-readable account of the number.

    ⚠️ `rationale` is free text from a third-party model. Render it with `textContent`,
    never `innerHTML`.
    """
    for d in _load(raw):
        if not isinstance(d, dict):
            continue
        verdict = d.get("_vlm_verdict")
        if not isinstance(verdict, dict):
            continue
        try:
            return {
                "is_pothole": bool(verdict["is_pothole"]),
                "confidence": float(verdict.get("confidence") or 0.0),
                "severity": verdict.get("severity"),
                "rationale": verdict.get("rationale") or "",
                "model_id": verdict.get("model_id") or "",
            }
        except (KeyError, TypeError, ValueError):
            return None
    return None


def count_detection_boxes(raw: Any) -> int:
    """Boxes only -- what `jsonb_array_length()` gets wrong once hybrid runs."""
    return len(parse_detection_boxes(raw))
