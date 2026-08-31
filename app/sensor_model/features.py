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

# Every feature the Isolation Forest gate can be built from, in canonical order.
# This is the menu, not the selection — see OUTLIER_FEATURES below.
OUTLIER_FEATURE_MENU = ("ratio", "gbar", "magnitude", "accel_std", "speed_mps")

# What models fitted before migration 014 used: the whole menu. Kept as a named
# constant because those models are still loadable and must be scored with the
# feature set they were fitted on, not today's default.
LEGACY_OUTLIER_FEATURES = OUTLIER_FEATURE_MENU

# The default selection, and the reason this is configurable at all.
#
# `ratio`, `gbar` and `magnitude` are the features on which potholes separate
# from everything else by 14-15x. An unsupervised outlier detector fitted on
# them learns "pothole" and reports it as "outlier": measured over 140 rows the
# gate removed 139, and on the collected `pothole_db` it flags 285 of 286
# pothole-classed observations. The cluster member gate is
# `sensor_class = 'pothole' AND sensor_is_outlier IS NOT TRUE`, so that is the
# whole crowd pipeline starved to one row.
#
# Tuning `contamination` cannot fix it — below 0.05 the gate flags nothing but
# potholes, so the dial runs between "no gate" and "no potholes". The answer is
# a class-neutral feature set: `accel_std` (baseline road noise) and `speed_mps`
# carry no pothole signal, so the gate flags roughly uniformly across classes
# and keeps 122 of 140 at the unchanged contamination 0.1.
#
# Recorded in docs/phases/phase-2.1-fusion-engine-plan.md "Open items" #2.
OUTLIER_FEATURES = ("accel_std", "speed_mps")


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
    names: tuple[str, ...] = OUTLIER_FEATURES,
) -> list[float]:
    """The vector the Isolation Forest gate operates on, in `names` order.

    `names` is explicit rather than read from settings so this module stays pure
    and so a model is always scored with the feature set it was *fitted* with —
    which is not necessarily today's configured default. Callers pass
    `model.outlier_features`.
    """
    available = {
        "ratio": compute_ratio(magnitude, accel_std),
        "gbar": gbar_in_max if gbar_in_max is not None else 0.0,
        "magnitude": magnitude if magnitude is not None else 0.0,
        "accel_std": accel_std if accel_std is not None else 0.0,
        "speed_mps": speed_mps if speed_mps is not None else 0.0,
    }
    return [available[n] for n in names]


def parse_outlier_features(raw: str) -> tuple[str, ...]:
    """Parse the comma-separated SENSOR_OUTLIER_FEATURES setting.

    Rejects unknown names loudly at startup rather than at fit time: a typo here
    would otherwise surface as a KeyError inside a background job, every hour,
    with the gate silently never re-fitting.
    """
    names = tuple(part.strip() for part in raw.split(",") if part.strip())
    if not names:
        raise ValueError("SENSOR_OUTLIER_FEATURES is empty; give at least one feature")
    unknown = [n for n in names if n not in OUTLIER_FEATURE_MENU]
    if unknown:
        raise ValueError(
            f"Unknown outlier feature(s) {unknown}; "
            f"valid names are {list(OUTLIER_FEATURE_MENU)}"
        )
    return names


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
