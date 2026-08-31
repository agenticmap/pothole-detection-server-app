"""Unit tests for feature engineering and the severity proxy (no DB)."""

import pytest

from app.sensor_model import features as feat


def test_compute_ratio_basic():
    assert feat.compute_ratio(3.0, 1.0) == 3.0
    assert feat.compute_ratio(6.0, 2.0) == 3.0


def test_compute_ratio_guards_zero_and_none():
    assert feat.compute_ratio(3.0, 0.0) == 0.0
    assert feat.compute_ratio(3.0, None) == 0.0
    assert feat.compute_ratio(None, 1.0) == 0.0


def test_classifier_features_order_and_nulls():
    assert feat.classifier_features(3.0, 1.0, 2.0) == [3.0, 2.0]
    assert feat.classifier_features(3.0, 1.0, None) == [3.0, 0.0]


def test_outlier_features_defaults_to_the_class_neutral_pair():
    # ratio/gbar/magnitude are the features potholes separate on by 14-15x, so a
    # gate fitted on them flags potholes as outliers. The default must not
    # include them.
    assert feat.OUTLIER_FEATURES == ("accel_std", "speed_mps")
    v = feat.outlier_features(3.0, 1.0, 2.0, 12.0)
    assert v == [1.0, 12.0]                       # accel_std, speed_mps
    assert len(v) == len(feat.OUTLIER_FEATURES)


def test_outlier_features_legacy_set_is_unchanged():
    # Models fitted before migration 014 are scored with this exact vector.
    v = feat.outlier_features(3.0, 1.0, 2.0, 12.0, feat.LEGACY_OUTLIER_FEATURES)
    assert v == [3.0, 2.0, 3.0, 1.0, 12.0]
    assert feat.LEGACY_OUTLIER_FEATURES == feat.OUTLIER_FEATURE_MENU


def test_outlier_features_respects_requested_order():
    assert feat.outlier_features(3.0, 1.0, 2.0, 12.0, ("speed_mps", "accel_std")) == [
        12.0,
        1.0,
    ]


def test_parse_outlier_features_trims_and_validates():
    assert feat.parse_outlier_features(" accel_std , speed_mps ") == (
        "accel_std",
        "speed_mps",
    )
    with pytest.raises(ValueError, match="Unknown outlier feature"):
        feat.parse_outlier_features("accel_std,not_a_feature")
    with pytest.raises(ValueError, match="empty"):
        feat.parse_outlier_features("  ,  ")


def test_severity_clamps_to_one():
    # 2 * 10 / max(10, 5) = 2.0 -> clamped to 1.0
    assert feat.severity(10.0, 10.0, speed_ref=5.0, scale=2.0) == 1.0


def test_severity_lower_speed_means_higher_severity():
    fast = feat.severity(2.0, 20.0, speed_ref=5.0, scale=2.0)   # 4/20 = 0.2
    slow = feat.severity(2.0, 2.0, speed_ref=5.0, scale=2.0)    # 4/5  = 0.8 (speed floored to ref)
    assert fast == 0.2
    assert slow == 0.8
    assert slow > fast


def test_severity_zero_magnitude():
    assert feat.severity(0.0, 10.0, speed_ref=5.0, scale=2.0) == 0.0
    assert feat.severity(None, 10.0, speed_ref=5.0, scale=2.0) == 0.0
