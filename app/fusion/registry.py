"""Engine selection — returns the right FusionEngine for the current state.

When an active SensorModel exists, the configured primary engine
(fusion.matlab_port_v1) is used with that model's scored sensor probability.
Otherwise the heuristic fallback (fusion.python_v1) carries the decision until a
model is fit. New engine versions add a branch here only — orchestration is
unchanged.
"""

from __future__ import annotations

from app.config import settings
from app.fusion.engine import FusionEngine
from app.fusion.matlab_port_v1 import VERSION as MATLAB_V1
from app.fusion.matlab_port_v1 import MatlabPortV1Engine
from app.fusion.python_v1 import VERSION as PYTHON_V1
from app.fusion.python_v1 import PythonV1Engine
from app.sensor_model.model import SensorModel


def get_engine(model: SensorModel | None) -> FusionEngine:
    """Pick the engine: primary if a model is active, else heuristic fallback."""
    if model is not None and settings.fusion_engine_version == MATLAB_V1:
        return MatlabPortV1Engine(
            w_s=settings.fusion_w_s,
            w_v=settings.fusion_w_v,
            sensor_model_version=model.model_version,
        )
    # Fallback (no model yet, or explicitly configured to the heuristic).
    return PythonV1Engine(
        w_s=settings.fusion_w_s,
        w_v=settings.fusion_w_v,
        ratio_mean=settings.fallback_ratio_mean,
        ratio_std=settings.fallback_ratio_std,
        severity_speed_ref=settings.severity_speed_ref,
        severity_scale=settings.severity_scale,
    )


__all__ = ["get_engine", "MATLAB_V1", "PYTHON_V1"]
