"""Fusion engine contract — frozen dataclasses + a structural Protocol.

The engine is addressed as a black box so implementations swap with no change to
the orchestration: `fusion.matlab_port_v1` (the ported sensor model feeding the
sigmoid fusion) is first; `fusion.python_v1` is the cold-start heuristic; a
Phase-3 CNN can drop in behind the same Protocol.

All math here is pure and deterministic (clamped logit/sigmoid over IEEE-754).
`runtime_ms` lives only in `debug` and is never persisted into a confidence
column, so it cannot affect byte-identical reruns.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

_EPS = 1e-6


def sigmoid(x: float) -> float:
    # Numerically stable logistic.
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def logit(p: float) -> float:
    """Inverse sigmoid with clamping to keep the result finite."""
    p = min(1.0 - _EPS, max(_EPS, p))
    return math.log(p / (1.0 - p))


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


@dataclass(frozen=True)
class FusionInput:
    """One candidate (frame, observation) pairing plus the observation's
    scored sensor signal."""

    # Raw observation fields (let the heuristic fallback derive its own term).
    magnitude: float | None
    accel_std: float | None
    gbar_in_max: float | None
    speed_mps: float | None
    # Scored sensor signal (present when an active SensorModel scored the obs).
    sensor_p_pothole: float | None
    sensor_severity: float | None
    # Visual signal from the paired frame.
    visual_confidence: float | None
    # Pairing geometry.
    delta_ms: int
    delta_m: float


@dataclass(frozen=True)
class FusionDebug:
    model_id: str
    engine_version: str
    runtime_ms: float


@dataclass(frozen=True)
class FusionOutput:
    fused_confidence: float
    severity: float
    feature_vector: tuple[float, ...] | None
    debug: FusionDebug


@runtime_checkable
class FusionEngine(Protocol):
    version: str

    def weights(self) -> dict[str, float]: ...

    def fuse(self, inp: FusionInput) -> FusionOutput: ...
