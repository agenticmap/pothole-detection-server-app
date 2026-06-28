"""Unit tests for the sensor scorer (no DB).

Builds a SensorModel by hand (no sklearn fit needed for the classification path —
scoring is pure scipy math over stored components).
"""

import numpy as np
from sklearn.ensemble import IsolationForest

from app.sensor_model.model import (
    CLASS_NOT,
    CLASS_POTHOLE,
    SensorModel,
    SeverityCalibration,
    Standardization,
)
from app.sensor_model.score import score_observation

_IDENTITY_STD = Standardization(ratio_mean=0.0, ratio_std=1.0, gbar_mean=0.0, gbar_std=1.0)
_SEV = SeverityCalibration(speed_ref=5.0, scale=2.0)


def _model(iforest=None) -> SensorModel:
    # Three components in raw [ratio, gbar] space (identity standardization):
    # not ~ (1,1), crack ~ (3,3), pothole ~ (6,6).
    sigma = [[0.5, 0.0], [0.0, 0.5]]
    components = [
        {"mu": [1.0, 1.0], "sigma": sigma, "weight": 1 / 3},
        {"mu": [3.0, 3.0], "sigma": sigma, "weight": 1 / 3},
        {"mu": [6.0, 6.0], "sigma": sigma, "weight": 1 / 3},
    ]
    class_map = {0: CLASS_NOT, 1: "crack", 2: CLASS_POTHOLE}
    return SensorModel(
        model_version="test-model",
        standardization=_IDENTITY_STD,
        class_map=class_map,
        severity_calib=_SEV,
        k=3,
        n_observations=300,
        components=components,
        iforest=iforest,
    )


def test_scores_pothole_for_high_ratio_gbar():
    m = _model()
    res = score_observation(m, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0)
    assert res.sensor_class == CLASS_POTHOLE
    assert res.p_pothole > 0.9


def test_scores_not_for_low_ratio_gbar():
    m = _model()
    res = score_observation(m, magnitude=1.0, accel_std=1.0, gbar_in_max=1.0, speed_mps=12.0)
    assert res.sensor_class == CLASS_NOT
    assert res.p_pothole < 0.1


def test_score_is_deterministic():
    m = _model()
    a = score_observation(m, magnitude=4.0, accel_std=1.0, gbar_in_max=4.0, speed_mps=10.0)
    b = score_observation(m, magnitude=4.0, accel_std=1.0, gbar_in_max=4.0, speed_mps=10.0)
    assert a == b


def test_no_iforest_means_not_outlier():
    m = _model(iforest=None)
    res = score_observation(m, magnitude=6.0, accel_std=1.0, gbar_in_max=6.0, speed_mps=12.0)
    assert res.is_outlier is False


def test_iforest_flags_extreme_outlier():
    # Fit a forest on a tight normal blob, then score a far-away point.
    rng = np.random.default_rng(0)
    normal = rng.normal(loc=[3, 3, 3, 1, 12], scale=0.3, size=(200, 5))
    iforest = IsolationForest(contamination=0.05, random_state=42).fit(normal)
    m = _model(iforest=iforest)
    res = score_observation(m, magnitude=500.0, accel_std=0.01, gbar_in_max=400.0, speed_mps=0.0)
    assert res.is_outlier is True
