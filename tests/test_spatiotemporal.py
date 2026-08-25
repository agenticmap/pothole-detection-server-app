"""Unit tests for the spatiotemporal crowd-fusion maths (Phase 2.2c).

Pure — no Postgres, no fixtures. These pin the behaviour the paper's §4.5 claims,
including two of its worked numbers, so a refactor cannot quietly change the
weighting scheme.
"""

import numpy as np
import pytest

from app.fusion.spatiotemporal import (
    combine_weights,
    fit_dirichlet_multinomial,
    integrate_cluster,
    rbf_weights,
)

CLASSES = ["pothole", "crack", "not"]

# Defaults mirroring the config knobs: 1 m and 1 hour.
FLOOR_M = 1.0
FLOOR_S = 3600.0


def _integrate(dists, spatial, temporal):
    return integrate_cluster(
        class_labels=CLASSES,
        member_distributions=dists,
        spatial_distances_m=spatial,
        temporal_distances_s=temporal,
        sigma_floor_m=FLOOR_M,
        sigma_floor_s=FLOOR_S,
    )


class TestRbfWeights:
    def test_weight_falls_off_with_distance(self):
        w = rbf_weights([0.0, 2.0, 5.0, 12.0], sigma_floor=FLOOR_M)
        assert w == sorted(w, reverse=True)
        assert w[0] == pytest.approx(1.0)  # on the centroid

    def test_single_member_is_not_a_division_by_zero(self):
        """One member → population std is exactly 0 → γ would be infinite."""
        assert rbf_weights([0.0], sigma_floor=FLOOR_M) == [pytest.approx(1.0)]

    def test_identical_distances_hit_the_floor(self):
        """Zero spread is the other σ=0 route: every member equally far out."""
        w = rbf_weights([4.0, 4.0, 4.0], sigma_floor=FLOOR_M)
        assert all(np.isfinite(x) for x in w)
        assert len(set(w)) == 1

    def test_a_tight_cluster_discriminates_more_sharply(self):
        """γ comes from the cluster's own spread, so scale is relative, not absolute."""
        tight = rbf_weights([0.0, 1.0, 2.0], sigma_floor=0.01)
        loose = rbf_weights([0.0, 50.0, 100.0], sigma_floor=0.01)
        # Same *relative* geometry → same weights. That is the point of per-cluster γ.
        assert tight == pytest.approx(loose)

    def test_rejects_nonsense_input(self):
        with pytest.raises(ValueError, match="non-negative"):
            rbf_weights([-1.0, 2.0], sigma_floor=FLOOR_M)
        with pytest.raises(ValueError, match="finite"):
            rbf_weights([float("nan")], sigma_floor=FLOOR_M)

    def test_empty_is_empty(self):
        assert rbf_weights([], sigma_floor=FLOOR_M) == []


class TestCombineWeights:
    def test_weights_sum_to_one(self):
        w = combine_weights([1.0, 0.5, 0.1], [0.2, 1.0, 0.3])
        assert sum(w) == pytest.approx(1.0)

    def test_underflow_falls_back_to_uniform(self):
        """Reachable: the paper reports a member receiving ~zero spatial weight."""
        w = combine_weights([0.0, 0.0, 0.0], [0.0, 0.0, 0.0])
        assert w == pytest.approx([1 / 3, 1 / 3, 1 / 3])

    def test_mismatched_lengths_are_rejected(self):
        with pytest.raises(ValueError, match="lengths differ"):
            combine_weights([1.0, 2.0], [1.0])


class TestDirichletFit:
    def test_mean_tracks_the_weighted_evidence(self):
        # Three members, all leaning 'pothole'.
        matrix = np.array([[0.8, 0.15, 0.05]] * 3)
        mean, concentration, _ = fit_dirichlet_multinomial(matrix)
        assert mean[0] == pytest.approx(0.8, abs=0.05)
        assert sum(mean) == pytest.approx(1.0)
        assert concentration > 0.0

    def test_agreement_yields_higher_concentration_than_disagreement(self):
        """Directionally sound even though the magnitude is not.

        Disagreement gives a small finite concentration; agreement diverges toward
        the bound. So the ordering is meaningful while the agreeing value is not an
        estimate of anything -- see the module docstring.
        """
        agree = np.array([[0.8, 0.1, 0.1]] * 4)
        disagree = np.array([[0.8, 0.1, 0.1], [0.1, 0.8, 0.1],
                             [0.1, 0.1, 0.8], [0.34, 0.33, 0.33]])
        _, c_agree, _ = fit_dirichlet_multinomial(agree)
        _, c_disagree, _ = fit_dirichlet_multinomial(disagree)
        assert c_agree > c_disagree

    def test_all_zero_matrix_is_uniform_not_a_crash(self):
        mean, concentration, converged = fit_dirichlet_multinomial(np.zeros((3, 3)))
        assert mean == pytest.approx([1 / 3, 1 / 3, 1 / 3])
        assert concentration == 0.0
        assert converged is False

    def test_rejects_bad_shapes_and_values(self):
        with pytest.raises(ValueError, match="2-D matrix"):
            fit_dirichlet_multinomial(np.array([1.0, 2.0]))
        with pytest.raises(ValueError, match="non-negative"):
            fit_dirichlet_multinomial(np.array([[-0.5, 1.0]]))


class TestIntegrateCluster:
    def test_recent_and_near_members_dominate(self):
        """§4.5's core claim, as a behavioural assertion.

        Two members disagree completely. One is on the centroid and detected now;
        the other is 30 m away and three weeks old. The result must follow the
        first, which a plain average could not do.
        """
        result = _integrate(
            dists=[[0.9, 0.05, 0.05], [0.05, 0.05, 0.9]],
            spatial=[0.0, 30.0],
            temporal=[0.0, 21 * 24 * 3600.0],
        )
        assert result.distribution["pothole"] > result.distribution["not"]
        assert result.weights[0] > result.weights[1]

    def test_the_paper_s_thirteen_metre_member_is_nearly_ignored(self):
        """Table 8: the member ~13 m out 'allocated zero weight' spatially."""
        w = rbf_weights([0.5, 1.2, 13.0, 2.0], sigma_floor=FLOOR_M)
        assert w[2] < 0.05
        assert w[2] < min(w[0], w[1], w[3]) / 10

    def test_the_latest_detection_gets_the_highest_temporal_weight(self):
        """Table 8: 'the most recent detections ... allocated the highest temporal weight'."""
        day = 24 * 3600.0
        w = rbf_weights([0.0, 2 * day, 7 * day, 9 * day], sigma_floor=FLOOR_S)
        assert w[0] == max(w)
        assert w == sorted(w, reverse=True)

    def test_result_is_order_independent(self):
        """Members arrive in whatever order the query returns; the answer must not."""
        dists = [[0.7, 0.2, 0.1], [0.2, 0.7, 0.1], [0.4, 0.4, 0.2]]
        spatial = [1.0, 5.0, 9.0]
        temporal = [0.0, 3600.0, 7200.0]
        a = _integrate(dists, spatial, temporal)

        order = [2, 0, 1]
        b = _integrate(
            [dists[i] for i in order],
            [spatial[i] for i in order],
            [temporal[i] for i in order],
        )
        for cls in CLASSES:
            assert a.distribution[cls] == pytest.approx(b.distribution[cls], abs=1e-9)

    def test_single_member_cluster_returns_that_member(self):
        result = _integrate([[0.6, 0.3, 0.1]], [0.0], [0.0])
        assert result.weights == [pytest.approx(1.0)]
        assert result.distribution["pothole"] == pytest.approx(0.6, abs=0.05)

    def test_distribution_is_normalised(self):
        result = _integrate(
            [[0.5, 0.3, 0.2], [0.1, 0.1, 0.8], [0.9, 0.05, 0.05]],
            [0.0, 4.0, 8.0],
            [0.0, 1800.0, 3600.0],
        )
        assert sum(result.distribution.values()) == pytest.approx(1.0)
        assert all(0.0 <= v <= 1.0 for v in result.distribution.values())

    def test_the_paper_s_method_does_not_accumulate_across_devices(self):
        """Recorded because it is counter-intuitive and was initially assumed otherwise.

        Three devices agreeing at 0.6 integrate to the SAME 0.6 as one device would.
        The paper's contribution is spatiotemporal weighting, not evidence
        combination. Corroboration is carried by distinct_devices, not by this
        distribution. Pinned so nobody later "fixes" the weighting expecting it to
        make agreement raise confidence.
        """
        lone = _integrate([[0.6, 0.2, 0.2]], [0.0], [0.0])
        crowd = _integrate([[0.6, 0.2, 0.2]] * 3, [0.0, 1.0, 2.0], [0.0, 60.0, 120.0])
        assert crowd.distribution["pothole"] == pytest.approx(
            lone.distribution["pothole"], abs=1e-9
        )

    def test_a_prior_makes_corroboration_raise_confidence(self):
        """The opt-in extension: shrinkage toward uniform that corroboration overcomes."""
        lone = integrate_cluster(
            class_labels=CLASSES, member_distributions=[[0.6, 0.2, 0.2]],
            spatial_distances_m=[0.0], temporal_distances_s=[0.0],
            sigma_floor_m=FLOOR_M, sigma_floor_s=FLOOR_S, prior_concentration=1.0,
        )
        crowd = integrate_cluster(
            class_labels=CLASSES, member_distributions=[[0.6, 0.2, 0.2]] * 3,
            spatial_distances_m=[0.0, 1.0, 2.0], temporal_distances_s=[0.0, 60.0, 120.0],
            sigma_floor_m=FLOOR_M, sigma_floor_s=FLOOR_S, prior_concentration=1.0,
        )
        # Both shrink below the observed 0.6, but the crowd shrinks less.
        assert lone.distribution["pothole"] < crowd.distribution["pothole"] < 0.6
        # And the prior never invents confidence the members did not report.
        assert crowd.distribution["pothole"] < 0.6

    def test_prior_of_zero_is_the_papers_result(self):
        args = dict(
            class_labels=CLASSES, member_distributions=[[0.7, 0.2, 0.1]] * 2,
            spatial_distances_m=[0.0, 3.0], temporal_distances_s=[0.0, 600.0],
            sigma_floor_m=FLOOR_M, sigma_floor_s=FLOOR_S,
        )
        assert integrate_cluster(**args, prior_concentration=0.0).distribution[
            "pothole"
        ] == pytest.approx(0.7)

    def test_negative_prior_is_rejected(self):
        with pytest.raises(ValueError, match="prior_concentration"):
            integrate_cluster(
                class_labels=CLASSES, member_distributions=[[0.5, 0.3, 0.2]],
                spatial_distances_m=[0.0], temporal_distances_s=[0.0],
                sigma_floor_m=FLOOR_M, sigma_floor_s=FLOOR_S,
                prior_concentration=-1.0,
            )

    def test_consensus_leaves_the_concentration_unidentified(self):
        """Guards the bound: identical members must not report a cap as an estimate."""
        result = _integrate([[0.6, 0.2, 0.2]] * 4, [0.0] * 4, [0.0] * 4)
        assert result.converged is False
        assert result.concentration <= 1.0e3

    def test_misaligned_inputs_are_rejected(self):
        with pytest.raises(ValueError, match="must align"):
            _integrate([[0.5, 0.3, 0.2]], [0.0, 1.0], [0.0])
        with pytest.raises(ValueError, match="class labels"):
            _integrate([[0.5, 0.5]], [0.0], [0.0])

    def test_empty_cluster_is_rejected(self):
        with pytest.raises(ValueError, match="at least one member"):
            _integrate([], [], [])
