"""SensorModel — the in-memory representation of a fitted, versioned classifier.

A SensorModel bundles everything the scorer needs:
  - standardization stats for the [ratio, gbar] classifier features,
  - the fitted GaussianMixture (component posteriors -> class posteriors),
  - the component->class map (which GMM component is pothole/crack/not),
  - the fitted IsolationForest outlier gate,
  - the severity calibration,
  - audit metadata (version, k, bic, n_observations, sklearn_version).

The sklearn objects (`gmm`, `iforest`) come from the joblib blob; the JSONB
parameters persisted alongside are the human-readable / reproducible mirror used
for audit. Classes are the fixed taxonomy from the research.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

CLASS_POTHOLE = "pothole"
CLASS_CRACK = "crack"
CLASS_NOT = "not"
CLASSES = (CLASS_NOT, CLASS_CRACK, CLASS_POTHOLE)  # ascending "energy" order


@dataclass(frozen=True)
class Standardization:
    """Per-feature mean/std used to z-score classifier features at score time."""

    ratio_mean: float
    ratio_std: float
    gbar_mean: float
    gbar_std: float

    def transform(self, ratio: float, gbar: float) -> list[float]:
        rs = self.ratio_std if self.ratio_std > 0 else 1.0
        gs = self.gbar_std if self.gbar_std > 0 else 1.0
        return [(ratio - self.ratio_mean) / rs, (gbar - self.gbar_mean) / gs]

    def to_jsonb(self) -> dict[str, Any]:
        return {
            "ratio": {"mean": self.ratio_mean, "std": self.ratio_std},
            "gbar": {"mean": self.gbar_mean, "std": self.gbar_std},
        }

    @classmethod
    def from_jsonb(cls, d: dict[str, Any]) -> Standardization:
        return cls(
            ratio_mean=d["ratio"]["mean"],
            ratio_std=d["ratio"]["std"],
            gbar_mean=d["gbar"]["mean"],
            gbar_std=d["gbar"]["std"],
        )


@dataclass(frozen=True)
class SeverityCalibration:
    speed_ref: float
    scale: float

    def to_jsonb(self) -> dict[str, Any]:
        return {"speed_ref": self.speed_ref, "scale": self.scale}

    @classmethod
    def from_jsonb(cls, d: dict[str, Any]) -> SeverityCalibration:
        return cls(speed_ref=d["speed_ref"], scale=d["scale"])


@dataclass
class SensorModel:
    """A fitted, versioned sensor classifier (one active at a time)."""

    model_version: str
    standardization: Standardization
    class_map: dict[int, str]          # GMM component index -> class label
    severity_calib: SeverityCalibration
    k: int
    n_observations: int
    bic: float | None = None
    sklearn_version: str | None = None
    # sklearn objects (from the joblib blob); not part of the audit JSONB.
    gmm: Any = None
    iforest: Any = None
    # Reproducible mirror of the GMM components (mu/sigma/weight per component).
    components: list[dict[str, Any]] = field(default_factory=list)
