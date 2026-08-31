"""Unit tests for the sensor-model fit (no DB).

Synthetic three-blob data in [ratio, gbar] space; the energy rule must label the
highest-||mu|| component 'pothole' and the lowest 'not', deterministically.
"""

import numpy as np
import pytest

from app.sensor_model import features as feat
from app.sensor_model.fit import FitError, fit_sensor_model
from app.sensor_model.model import CLASS_NOT, CLASS_POTHOLE, SeverityCalibration
from app.sensor_model.score import score_observation

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


def _classed_population(seed=7):
    """The shape the real data actually has, which is what makes the gate misfire.

    Potholes are a **sparse minority tail**, not an equal third: on `pothole_db`
    they are 286 of 4637 (6%), spread across a wide magnitude range, while the
    bulk of driving is a dense low-energy mass. `ratio`, `gbar` and `magnitude`
    separate them by an order of magnitude; `accel_std` (baseline road noise)
    and `speed_mps` are drawn from the same distribution for every class, which
    is precisely the claim that makes them class-neutral.

    Returns (rows, is_pothole_flags) so a test can ask which rows a gate flagged.
    """
    rng = np.random.default_rng(seed)
    rows, is_pothole = [], []

    def _row(ratio, gbar, pothole):
        accel_std = max(0.05, rng.normal(1.0, 0.25))         # class-independent
        speed = max(0.5, rng.normal(12.0, 3.0))              # class-independent
        rows.append(
            {
                "magnitude": ratio * accel_std,               # so ratio comes back out
                "accel_std": accel_std,
                "gbar_in_max": gbar,
                "speed_mps": speed,
            }
        )
        is_pothole.append(pothole)

    for _ in range(700):                                     # normal driving, dense
        _row(max(0.1, rng.normal(1.2, 0.35)), max(0.1, rng.normal(1.2, 0.35)), False)
    for _ in range(250):                                     # cracks, dense
        _row(max(0.1, rng.normal(3.5, 0.6)), max(0.1, rng.normal(3.5, 0.6)), False)
    for _ in range(60):                                      # potholes: sparse, diffuse
        _row(max(0.1, rng.normal(15.0, 5.0)), max(0.1, rng.normal(15.0, 5.0)), True)

    order = rng.permutation(len(rows))
    return [rows[i] for i in order], [is_pothole[i] for i in order]


def _pothole_flag_rate(rows, is_pothole, feature_names):
    """Fraction of the *potholes* the gate flags as outliers.

    This is the number the finding is stated in: the legacy gate removed 139 of
    140 potholes; the class-neutral one keeps 122 of 140. Share-of-all-flags
    would be dominated by the class balance instead.
    """
    model, _ = fit_sensor_model(
        rows,
        k_default=3,
        contamination=0.1,
        random_state=42,
        severity_calib=_SEV,
        outlier_feature_names=feature_names,
    )
    flagged = [
        score_observation(
            model,
            magnitude=r["magnitude"],
            accel_std=r["accel_std"],
            gbar_in_max=r["gbar_in_max"],
            speed_mps=r["speed_mps"],
        ).is_outlier
        for r in rows
    ]
    pothole_flags = [f for p, f in zip(is_pothole, flagged, strict=True) if p]
    return sum(pothole_flags) / len(pothole_flags)


def test_legacy_outlier_features_destroy_the_pothole_class():
    """The bug this setting exists to fix, pinned so it cannot silently return.

    The cluster member gate is `sensor_class = 'pothole' AND sensor_is_outlier
    IS NOT TRUE`, so a gate that flags potholes empties the crowd pipeline. On
    the real data it flagged 285 of 286.
    """
    rows, is_pothole = _classed_population()
    rate = _pothole_flag_rate(rows, is_pothole, feat.LEGACY_OUTLIER_FEATURES)
    assert rate > 0.8, f"expected the legacy gate to eat the pothole class, got {rate:.2f}"


def test_class_neutral_outlier_features_leave_potholes_alone():
    rows, is_pothole = _classed_population()
    rate = _pothole_flag_rate(rows, is_pothole, feat.OUTLIER_FEATURES)
    # contamination is 0.1, so a gate carrying no class signal should flag
    # potholes at roughly the base rate rather than preferentially.
    assert rate < 0.35, f"class-neutral gate still targets potholes: {rate:.2f}"


def test_fit_records_the_feature_set_it_used():
    rows, _ = _classed_population()
    model, _ = fit_sensor_model(
        rows,
        k_default=3,
        contamination=0.1,
        random_state=42,
        severity_calib=_SEV,
        outlier_feature_names=("accel_std", "speed_mps"),
    )
    assert model.outlier_features == ("accel_std", "speed_mps")
    assert model.iforest.n_features_in_ == 2
