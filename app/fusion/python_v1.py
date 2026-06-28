"""fusion.python_v1 — heuristic cold-start fallback.

Used before any SensorModel has been fit (near-empty DB pre-pilot). Derives a
sensor probability directly from the standardized ratio using fixed fallback
constants, then fuses with the visual term exactly like the primary engine. As
soon as a model is active, the registry returns the MATLAB-port engine instead.
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
from app.sensor_model import features as feat

VERSION = "fusion.python_v1"


class PythonV1Engine:
    version = VERSION

    def __init__(
        self,
        w_s: float = 0.5,
        w_v: float = 0.5,
        ratio_mean: float = 3.0,
        ratio_std: float = 1.0,
        severity_speed_ref: float = 5.0,
        severity_scale: float = 2.0,
    ):
        self.w_s = w_s
        self.w_v = w_v
        self.ratio_mean = ratio_mean
        self.ratio_std = ratio_std if ratio_std > 0 else 1.0
        self.severity_speed_ref = severity_speed_ref
        self.severity_scale = severity_scale

    def weights(self) -> dict[str, float]:
        return {
            "w_s": self.w_s,
            "w_v": self.w_v,
            "ratio_mean": self.ratio_mean,
            "ratio_std": self.ratio_std,
        }

    def fuse(self, inp: FusionInput) -> FusionOutput:
        start = time.perf_counter()

        ratio = feat.compute_ratio(inp.magnitude, inp.accel_std)
        z = (ratio - self.ratio_mean) / self.ratio_std
        p_sensor = sigmoid(z)

        terms = [self.w_s * logit(clamp01(p_sensor))]
        if inp.visual_confidence is not None:
            terms.append(self.w_v * logit(clamp01(inp.visual_confidence)))
        fused = sigmoid(sum(terms))

        severity = feat.severity(
            inp.magnitude,
            inp.speed_mps,
            speed_ref=self.severity_speed_ref,
            scale=self.severity_scale,
        )

        runtime_ms = (time.perf_counter() - start) * 1000.0
        return FusionOutput(
            fused_confidence=fused,
            severity=severity,
            feature_vector=None,
            debug=FusionDebug(
                model_id="heuristic",
                engine_version=self.version,
                runtime_ms=runtime_ms,
            ),
        )
