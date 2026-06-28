"""Feature engineering — port of finding.m, plus an IRI-style severity proxy.

The 2017 research clustered on two engineered features that recur across every
MATLAB script:
  - ratio = PotMag / NormStd  (peak magnitude / baseline-noise std)
  - GbarInMax                 (gravity-bar at max)

These map directly to existing asset_observation columns: ratio is
`magnitude / accel_std`, and gbar is `gbar_in_max`. Orientation correction
(Rotation.m) already happens on-device, so the server consumes pre-corrected
values.

All functions here are pure (no DB, no I/O) so they unit-test trivially.
"""

from __future__ import annotations

# Classification feature vector fed to the GMM (order is load-bearing — it must
# match the order used at fit time when standardization stats were computed).
CLASSIFIER_FEATURES = ("ratio", "gbar")

# Richer feature vector fed to the Isolation Forest outlier gate.
OUTLIER_FEATURES = ("ratio", "gbar", "magnitude", "accel_std", "speed_mps")


def compute_ratio(magnitude: float | None, accel_std: float | None) -> float:
    """ratio = magnitude / accel_std (peak-to-baseline-noise ratio).

    Guards a zero/None baseline: a non-positive accel_std means no usable noise
    floor, so the ratio is treated as 0.0 (neutral — the event carries no
    discriminating sensor signal).
    """
    if magnitude is None or accel_std is None or accel_std <= 0.0:
        return 0.0
    return magnitude / accel_std


def classifier_features(
    magnitude: float | None,
    accel_std: float | None,
    gbar_in_max: float | None,
) -> list[float]:
    """The 2-D [ratio, gbar] vector the GMM classifier operates on."""
    return [
        compute_ratio(magnitude, accel_std),
        gbar_in_max if gbar_in_max is not None else 0.0,
    ]


def outlier_features(
    magnitude: float | None,
    accel_std: float | None,
    gbar_in_max: float | None,
    speed_mps: float | None,
) -> list[float]:
    """The richer vector the Isolation Forest gate operates on."""
    return [
        compute_ratio(magnitude, accel_std),
        gbar_in_max if gbar_in_max is not None else 0.0,
        magnitude if magnitude is not None else 0.0,
        accel_std if accel_std is not None else 0.0,
        speed_mps if speed_mps is not None else 0.0,
    ]


def severity(
    magnitude: float | None,
    speed_mps: float | None,
    *,
    speed_ref: float,
    scale: float,
) -> float:
    """IRI-style, label-free severity proxy in [0, 1].

    For a given road defect, a faster vehicle produces a larger acceleration
    spike. To estimate the intrinsic defect severity we normalize the measured
    impact by speed: a large jolt at low speed implies a rougher defect. A
    `speed_ref` floor avoids divide-by-zero / blow-up at crawl speed.

        severity = clamp(scale * magnitude / max(speed, speed_ref), 0, 1)

    This is a deliberately simple proxy; the raw [180,10] window stored in
    `raw_window_b64` is available for a fuller double-integration IRI later.
    """
    if magnitude is None or magnitude <= 0.0:
        return 0.0
    speed = speed_mps if (speed_mps is not None and speed_mps > 0.0) else 0.0
    denom = max(speed, speed_ref)
    raw = scale * magnitude / denom
    return max(0.0, min(1.0, raw))
