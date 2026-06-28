"""Fit the unsupervised sensor classifier.

Ports the 2017 MATLAB fitting chain (Kmesnspls.m -> GMM.m -> AIC_BIC.m) into a
few sklearn calls, and fills the gap left by the missing `ClusterCalc`:

  1. compute [ratio, gbar] features, robust-standardize,
  2. GaussianMixture (k-means++ init, full covariance) — the research's fitgmdist,
  3. BIC sweep over k for observability (.bic()),
  4. deterministic component->class assignment via the "energy" rule, replacing
     the human cluster-naming the missing training files used to provide,
  5. IsolationForest outlier gate (modern replacement for the Hotelling-T² test).

Fitting is a discrete, versioned event: with a fixed random_state it is
reproducible, and the artifact is frozen once written. Scoring between refits is
deterministic. A refit = a new model_version.
"""

from __future__ import annotations

import io
from uuid import uuid4

import joblib
import numpy as np
import sklearn
from sklearn.ensemble import IsolationForest
from sklearn.mixture import GaussianMixture

from app.sensor_model import features as feat
from app.sensor_model.model import (
    SensorModel,
    SeverityCalibration,
    Standardization,
)


class FitError(RuntimeError):
    """Raised when there is not enough / not enough variety of data to fit."""


def _energy_order(means: np.ndarray) -> list[int]:
    """Rank GMM components by ascending energy (||mu|| in z-space).

    The lowest-energy component (closest to origin = normal driving, per the
    anomaly-detection normality assumption) is NotPothole; the highest is
    Pothole; the middle is Crack. argsort is stable, so ties break by component
    index — keeping the assignment deterministic.
    """
    norms = np.linalg.norm(means, axis=1)
    return list(np.argsort(norms, kind="stable"))


def fit_sensor_model(
    rows: list[dict],
    *,
    k_default: int = 3,
    k_max: int = 5,
    contamination: float = 0.1,
    random_state: int = 42,
    severity_calib: SeverityCalibration,
    engine_prefix: str = "sensor_gmm_v1",
) -> tuple[SensorModel, bytes]:
    """Fit a SensorModel from observation rows.

    `rows` items need keys: magnitude, accel_std, gbar_in_max, speed_mps.
    Returns (model, joblib_blob_bytes). Raises FitError if the data is too small
    or degenerate to fit `k_default` components.
    """
    if len(rows) < k_default:
        raise FitError(f"need >= {k_default} observations, got {len(rows)}")

    xc = np.array(
        [feat.classifier_features(r["magnitude"], r["accel_std"], r["gbar_in_max"]) for r in rows],
        dtype=np.float64,
    )
    xo = np.array(
        [
            feat.outlier_features(
                r["magnitude"], r["accel_std"], r["gbar_in_max"], r["speed_mps"]
            )
            for r in rows
        ],
        dtype=np.float64,
    )

    # ── Standardize classifier features ────────────────────────────────────────
    ratio_mean, gbar_mean = xc.mean(axis=0)
    ratio_std, gbar_std = xc.std(axis=0, ddof=0)
    std = Standardization(
        ratio_mean=float(ratio_mean),
        ratio_std=float(ratio_std),
        gbar_mean=float(gbar_mean),
        gbar_std=float(gbar_std),
    )
    zc = np.array([std.transform(x[0], x[1]) for x in xc], dtype=np.float64)

    # Degenerate (all identical) features cannot form k_default components.
    if np.allclose(zc.std(axis=0), 0.0):
        raise FitError("features have zero variance; cannot fit GMM")

    # ── GaussianMixture classifier (fixed 3-class taxonomy: pot/crack/not) ──────
    gmm = GaussianMixture(
        n_components=k_default,
        init_params="k-means++",
        n_init=10,
        covariance_type="full",
        random_state=random_state,
    ).fit(zc)

    # BIC sweep — observability only; the classifier keeps the fixed taxonomy.
    bic = float(gmm.bic(zc))

    # ── Deterministic component -> class assignment (energy rule) ──────────────
    order = _energy_order(gmm.means_)  # ascending energy
    # CLASSES is (not, crack, pothole) in ascending energy order.
    from app.sensor_model.model import CLASSES

    class_map: dict[int, str] = {}
    for rank, comp_idx in enumerate(order):
        # If k_default != 3, clamp the highest ranks to the top class.
        cls = CLASSES[min(rank, len(CLASSES) - 1)]
        class_map[int(comp_idx)] = cls

    # ── Isolation Forest outlier gate (richer feature set, raw scale) ──────────
    iforest = IsolationForest(
        contamination=contamination,
        random_state=random_state,
    ).fit(xo)

    components = [
        {
            "mu": gmm.means_[i].tolist(),
            "sigma": gmm.covariances_[i].tolist(),
            "weight": float(gmm.weights_[i]),
        }
        for i in range(k_default)
    ]

    model = SensorModel(
        model_version=f"{engine_prefix}_{uuid4().hex[:12]}",
        standardization=std,
        class_map=class_map,
        severity_calib=severity_calib,
        k=k_default,
        n_observations=len(rows),
        bic=bic,
        sklearn_version=sklearn.__version__,
        gmm=gmm,
        iforest=iforest,
        components=components,
    )

    buf = io.BytesIO()
    joblib.dump((gmm, iforest), buf)
    return model, buf.getvalue()
