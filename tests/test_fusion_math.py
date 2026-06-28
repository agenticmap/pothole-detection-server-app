"""Unit tests for the fusion math + engines (no DB)."""

import math

from app.fusion.engine import FusionInput, logit, sigmoid
from app.fusion.matlab_port_v1 import MatlabPortV1Engine
from app.fusion.python_v1 import PythonV1Engine


def _input(**kw):
    base = dict(
        magnitude=3.0,
        accel_std=1.0,
        gbar_in_max=2.0,
        speed_mps=12.0,
        sensor_p_pothole=None,
        sensor_severity=None,
        visual_confidence=None,
        delta_ms=100,
        delta_m=3.0,
    )
    base.update(kw)
    return FusionInput(**base)


def test_sigmoid_logit_inverse():
    assert sigmoid(0.0) == 0.5
    assert abs(logit(0.5)) < 1e-12
    for p in (0.1, 0.3, 0.75, 0.9):
        assert abs(sigmoid(logit(p)) - p) < 1e-9


def test_matlab_port_neutral_inputs():
    eng = MatlabPortV1Engine(w_s=0.5, w_v=0.5)
    out = eng.fuse(_input(sensor_p_pothole=0.5, visual_confidence=0.5))
    assert abs(out.fused_confidence - 0.5) < 1e-9


def test_matlab_port_sensor_only_when_no_visual():
    eng = MatlabPortV1Engine(w_s=0.5, w_v=0.5)
    out = eng.fuse(_input(sensor_p_pothole=0.5, visual_confidence=None))
    assert abs(out.fused_confidence - 0.5) < 1e-9


def test_matlab_port_known_value():
    eng = MatlabPortV1Engine(w_s=0.5, w_v=0.5)
    out = eng.fuse(_input(sensor_p_pothole=0.88, visual_confidence=0.7))
    expected = sigmoid(0.5 * logit(0.88) + 0.5 * logit(0.7))
    assert abs(out.fused_confidence - expected) < 1e-9
    assert 0.0 <= out.fused_confidence <= 1.0


def test_matlab_port_passes_through_severity():
    eng = MatlabPortV1Engine()
    out = eng.fuse(_input(sensor_p_pothole=0.6, sensor_severity=0.42))
    assert out.severity == 0.42


def test_matlab_port_clamps_extremes():
    eng = MatlabPortV1Engine()
    out = eng.fuse(_input(sensor_p_pothole=1.0, visual_confidence=1.0))
    assert 0.0 < out.fused_confidence < 1.0
    assert math.isfinite(out.fused_confidence)


def test_python_v1_fallback_monotonic_in_ratio():
    eng = PythonV1Engine(w_s=0.5, w_v=0.5, ratio_mean=3.0, ratio_std=1.0)
    low = eng.fuse(_input(magnitude=2.0, accel_std=1.0))   # ratio 2 (below mean)
    high = eng.fuse(_input(magnitude=6.0, accel_std=1.0))  # ratio 6 (above mean)
    assert high.fused_confidence > low.fused_confidence


def test_python_v1_computes_severity():
    eng = PythonV1Engine(severity_speed_ref=5.0, severity_scale=2.0)
    out = eng.fuse(_input(magnitude=10.0, speed_mps=10.0))
    assert out.severity == 1.0  # clamped
