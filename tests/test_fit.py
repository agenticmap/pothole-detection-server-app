"""Unit tests for the sensor-model fit (no DB).

Synthetic three-blob data in [ratio, gbar] space; the energy rule must label the
highest-||mu|| component 'pothole' and the lowest 'not', deterministically.
"""

import numpy as np
import pytest

from app.sensor_model.fit import FitError, fit_sensor_model
from app.sensor_model.model import CLASS_NOT, CLASS_POTHOLE, SeverityCalibration

_SEV = SeverityCalibration(speed_ref=5.0, scale=2.0)


def _three_blobs(n=150, seed=0):
    """Build observation rows whose [ratio=mag/std, gbar] form 3 separated blobs."""
    rng = np.random.default_rng(seed)
    rows = []
    # (ratio_center, gbar_center): not / crack / pothole, ascending energy.
    for rc, gc in [(1.0, 1.0), (4.0, 4.0), (8.0, 8.0)]:
        for _ in range(n):
            ratio = max(0.1, rng.normal(rc, 0.3))
            gbar = max(0.1, rng.normal(gc, 0.3))
            # ratio = magnitude / accel_std; fix accel_std=1 so magnitude=ratio.
            rows.append(
                {
                    "magnitude": ratio,
                    "accel_std": 1.0,
                    "gbar_in_max": gbar,
                    "speed_mps": 12.0,
                }
            )
    rng.shuffle(rows)
    return rows


def test_fit_produces_three_classes():
    rows = _three_blobs()
    model, blob = fit_sensor_model(
        rows, k_default=3, contamination=0.1, random_state=42, severity_calib=_SEV
    )
    assert model.k == 3
    assert set(model.class_map.values()) == {CLASS_NOT, "crack", CLASS_POTHOLE}
    assert isinstance(blob, bytes) and len(blob) > 0
    assert model.iforest is not None


def test_energy_rule_assigns_highest_blob_to_pothole():
    rows = _three_blobs()
    model, _ = fit_sensor_model(
        rows, k_default=3, contamination=0.1, random_state=42, severity_calib=_SEV
    )
    # The component with the largest ||mu|| (in z-space) must be 'pothole'.
    means = np.array([c["mu"] for c in model.components])
    norms = np.linalg.norm(means, axis=1)
    highest = int(np.argmax(norms))
    lowest = int(np.argmin(norms))
    assert model.class_map[highest] == CLASS_POTHOLE
    assert model.class_map[lowest] == CLASS_NOT


def test_fit_is_deterministic():
    rows = _three_blobs()
    m1, _ = fit_sensor_model(rows, random_state=42, severity_calib=_SEV)
    m2, _ = fit_sensor_model(rows, random_state=42, severity_calib=_SEV)
    assert m1.class_map == m2.class_map
    means1 = np.array([c["mu"] for c in m1.components])
    means2 = np.array([c["mu"] for c in m2.components])
    assert np.allclose(means1, means2)


def test_fit_raises_on_too_few_rows():
    with pytest.raises(FitError):
        fit_sensor_model(
            [{"magnitude": 3.0, "accel_std": 1.0, "gbar_in_max": 2.0, "speed_mps": 12.0}],
            k_default=3,
            severity_calib=_SEV,
        )


def test_fit_raises_on_zero_variance():
    rows = [
        {"magnitude": 3.0, "accel_std": 1.0, "gbar_in_max": 2.0, "speed_mps": 12.0}
        for _ in range(50)
    ]
    with pytest.raises(FitError):
        fit_sensor_model(rows, k_default=3, severity_calib=_SEV)
