"""Unit tests for feature engineering and the severity proxy (no DB)."""

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


def test_outlier_features_shape():
    v = feat.outlier_features(3.0, 1.0, 2.0, 12.0)
    assert v == [3.0, 2.0, 3.0, 1.0, 12.0]
    assert len(v) == len(feat.OUTLIER_FEATURES)


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
