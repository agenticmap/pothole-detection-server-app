"""fusion.matlab_port_v1 — the primary engine.

The sensor term is P(pothole) produced by the ported MATLAB GMM/Gaussian-NB
classifier (app/sensor_model). It is combined with the camera's visual
confidence in logit space:

    fused_confidence = sigmoid(w_s · logit(P_sensor) + w_v · logit(P_visual))

(Logit-space late fusion, per roadmap §3.4 — cleaner than mixing a logit with a
raw probability. If a term is missing, it is simply dropped and the remaining
term carries the decision.) Severity is passed through from the sensor model's
IRI-style proxy.
"""

from __future__ import annotations

import time

from app.fusion.engine import (
    FusionDebug,
    FusionInput,
    FusionOutput,
    clamp01,
    logit,
    sigmoid,
)

VERSION = "fusion.matlab_port_v1"


class MatlabPortV1Engine:
    version = VERSION

    def __init__(self, w_s: float = 0.5, w_v: float = 0.5, sensor_model_version: str | None = None):
        self.w_s = w_s
        self.w_v = w_v
        self._model_id = sensor_model_version or "unknown"

    def weights(self) -> dict[str, float]:
        return {"w_s": self.w_s, "w_v": self.w_v}

    def fuse(self, inp: FusionInput) -> FusionOutput:
        start = time.perf_counter()

        terms: list[float] = []
        if inp.sensor_p_pothole is not None:
            terms.append(self.w_s * logit(clamp01(inp.sensor_p_pothole)))
        if inp.visual_confidence is not None:
            terms.append(self.w_v * logit(clamp01(inp.visual_confidence)))

        fused = sigmoid(sum(terms)) if terms else 0.5
        severity = inp.sensor_severity if inp.sensor_severity is not None else 0.0

        runtime_ms = (time.perf_counter() - start) * 1000.0
        return FusionOutput(
            fused_confidence=fused,
            severity=severity,
            feature_vector=None,
            debug=FusionDebug(
                model_id=self._model_id,
                engine_version=self.version,
                runtime_ms=runtime_ms,
            ),
        )
