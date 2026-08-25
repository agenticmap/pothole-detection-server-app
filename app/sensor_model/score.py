"""Score a single observation against a fitted SensorModel.

Classification is pure math over the stored GMM components (mu/sigma/weight) via
scipy — deterministic and independent of unpickling, so it unit-tests without
sklearn objects. The Isolation-Forest gate uses the fitted forest from the
joblib blob (replaces the Hotelling-T² test in ClusterFinding.m/CheckIfClustered.m).
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.stats import multivariate_normal

from app.sensor_model import features as feat
from app.sensor_model.model import CLASS_NOT, CLASS_POTHOLE, SensorModel


@dataclass(frozen=True)
class ScoreResult:
    sensor_class: str
    p_pothole: float
    severity: float
    is_outlier: bool
    # The whole posterior, not just its pothole component. Computed anyway by
    # _class_posteriors; Phase 2.2c's spatiotemporal integration needs the vector
    # because it combines distributions across cluster members, not scalars.
    # Empty when the posterior was degenerate (see the not-a-pothole branch below).
    class_probs: dict[str, float] = field(default_factory=dict)


def _class_posteriors(model: SensorModel, z: list[float]) -> dict[str, float]:
    """Component responsibilities grouped into class posteriors."""
    densities = []
    for comp in model.components:
        mu = np.asarray(comp["mu"], dtype=np.float64)
        sigma = np.asarray(comp["sigma"], dtype=np.float64)
        weight = float(comp["weight"])
        densities.append(weight * float(multivariate_normal.pdf(z, mean=mu, cov=sigma)))
    total = float(sum(densities))

    posteriors: dict[str, float] = {}
    if total <= 0.0 or not np.isfinite(total):
        # Degenerate (z is far from every component): no usable posterior.
        return posteriors
    for i, dens in enumerate(densities):
        cls = model.class_map[i]
        posteriors[cls] = posteriors.get(cls, 0.0) + dens / total
    return posteriors


def score_observation(
    model: SensorModel,
    *,
    magnitude: float | None,
    accel_std: float | None,
    gbar_in_max: float | None,
    speed_mps: float | None,
) -> ScoreResult:
    """Classify one observation: P(pothole), class, severity, outlier flag."""
    ratio, gbar = feat.classifier_features(magnitude, accel_std, gbar_in_max)
    z = model.standardization.transform(ratio, gbar)

    posteriors = _class_posteriors(model, z)

    severity = feat.severity(
        magnitude,
        speed_mps,
        speed_ref=model.severity_calib.speed_ref,
        scale=model.severity_calib.scale,
    )

    # ── Isolation Forest outlier gate ─────────────────────────────────────────
    is_outlier = False
    if model.iforest is not None:
        xo = np.asarray(
            [feat.outlier_features(magnitude, accel_std, gbar_in_max, speed_mps)],
            dtype=np.float64,
        )
        is_outlier = bool(model.iforest.predict(xo)[0] == -1)

    if not posteriors:
        # No usable class posterior -> treat as non-pothole outlier.
        return ScoreResult(
            sensor_class=CLASS_NOT, p_pothole=0.0, severity=severity, is_outlier=True
        )

    sensor_class = max(posteriors, key=posteriors.get)
    p_pothole = posteriors.get(CLASS_POTHOLE, 0.0)
    return ScoreResult(
        sensor_class=sensor_class,
        p_pothole=p_pothole,
        severity=severity,
        is_outlier=is_outlier,
        class_probs=posteriors,
    )
