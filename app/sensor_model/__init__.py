"""Sensor classification model — unsupervised accelerometer classifier.

Ported from the 2017 MATLAB research pipeline (k-means++ -> GMM -> BIC ->
Gaussian-NB classification). Produces a per-observation P(pothole), class label,
severity, and an Isolation-Forest outlier flag. See
docs/phases/phase-2.1-fusion-engine-plan.md for the full design.
"""
