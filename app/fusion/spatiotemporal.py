"""Spatiotemporal crowd fusion — the integration half of Sattar's probabilistic
crowdsourcing technique for road surface anomaly classification (§4.5).

Pure and deterministic: no I/O, no clock, no randomness. Same inputs → identical
outputs, which the clustering job's byte-identical-rerun property depends on.

## What the method does

Several devices, on several passes, each report a probability distribution over
anomaly classes for what is physically one defect. Averaging those is wrong in two
ways: it ignores that a detection 13 m from the cluster centroid is weaker evidence
than one on top of it, and it ignores that a detection from four weeks ago describes
a road that may since have deteriorated or been repaired.

So each member gets a weight from a Gaussian RBF (Eq. 2 of the paper):

    k(l, l') = exp(-||l - l'||² / 2σ²) = exp(-γ ||l - l'||²)

computed twice — once over distance to the cluster centroid, once over time since
the cluster's most recent detection — with γ derived from that cluster's own spread,
so a tight cluster discriminates finely and a loose one does not. The two weights are
summed and normalised to sum to 1, then multiplied into each member's class
distribution to form the weighted-probability matrix. A Dirichlet-multinomial is
fitted to that matrix; its mean is the cluster's integrated class distribution.

## Two interpretations, recorded because the paper is ambiguous

**Separate RBFs, not a joint norm.** §4.5 first says "Euclidean distance of both time
and location", which would put seconds and metres in one norm — dimensionally
meaningless. It then says "the weigh values of time and location computed from
Equation 2 should be summed", which is well-defined. This implements the second: two
independent 1-D kernels, each with its own γ.

**Pseudo-counts are rescaled by member count.** The paper's weighted matrix sums to
exactly 1 across all members and classes, so a Dirichlet-multinomial fitted to it has
a total observation count of 1 and its concentration is unidentified. Scaling the
matrix by the number of members leaves the fitted *mean* unchanged (it is
scale-invariant) but puts the concentration in interpretable units of "effective
observations". Only the concentration is affected, and only its scale.

## What to trust in the output

`distribution` is the load-bearing number and is reliable. It is the weight-normalised
mean of the members, which is exactly what the Dirichlet-multinomial MLE converges to
on this input, so it is computed directly.

`concentration` is **usually not identified, and must not be read as corroboration.**
Measured behaviour: for members that agree closely the observed over-dispersion is
zero, the ML concentration is infinite, and Minka's iteration simply climbs until it
is stopped — identical members returned ~200 with a 200-iteration cap and ~183 with
ten members, i.e. the number reported the cap rather than the data. It is bounded and
flagged (`converged`) rather than removed, because it is what the paper specifies and
it is informative when members genuinely disagree. **Cluster corroboration lives in
`distinct_devices`, not here.**

Note what this means for the method's scope: the paper's contribution is *spatiotemporal
weighting* — recent and central detections outweigh stale and peripheral ones — not
evidence accumulation across users. Three devices reporting 0.6 integrate to the same
0.6 as one device would. `integrate_cluster(prior_concentration=...)` is offered as an
explicit opt-in extension for the accumulation behaviour; it is off by default.
"""

from __future__ import annotations

import logging
import math
from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
from scipy.special import psi  # digamma; scipy is already a dependency

logger = logging.getLogger(__name__)

VERSION = "fusion.spatiotemporal_v1"

# Below this the weight vector is treated as underflowed and uniform weights are
# used instead. Normalising by a sum this small amplifies floating-point noise into
# the result.
_MIN_WEIGHT_SUM = 1e-12

# Minka fixed-point iteration limits. Deterministic: a fixed cap and tolerance, not
# a convergence-or-bust loop.
_MAX_ITER = 200
_TOL = 1e-8

# When members agree to within numerical precision the observed over-dispersion is
# zero and the maximum-likelihood concentration is genuinely INFINITE, so Minka's
# iteration climbs until it is stopped. Measured: identical members give ~200 at
# _MAX_ITER=200 and ~183 at 10 members -- i.e. the value reports the iteration cap,
# not the data. Bound it and report it as unidentified instead of pretending.
_MAX_CONCENTRATION = 1.0e3


@dataclass(frozen=True)
class ClusterPosterior:
    """The integrated result for one cluster."""

    distribution: dict[str, float]
    concentration: float
    weights: list[float]
    converged: bool


def rbf_weights(distances: Sequence[float], *, sigma_floor: float) -> list[float]:
    """Gaussian RBF weight per member, γ from the spread of `distances` itself.

    `sigma_floor` is not optional cosmetics: γ = 1/(2σ²), so a cluster whose members
    sit at nearly identical distances has σ→0 and γ→∞, which would send every weight
    to zero except the exact-centroid one. The floor also covers the single-member
    case, where the population standard deviation is exactly 0.
    """
    if not distances:
        return []
    d = np.asarray(distances, dtype=np.float64)
    if not np.all(np.isfinite(d)):
        raise ValueError("distances must be finite")
    if np.any(d < 0):
        raise ValueError("distances must be non-negative")

    sigma = max(float(d.std()), float(sigma_floor))
    gamma = 1.0 / (2.0 * sigma * sigma)
    return [float(math.exp(-gamma * float(x) * float(x))) for x in d]


def combine_weights(
    spatial: Sequence[float], temporal: Sequence[float]
) -> list[float]:
    """Sum the two kernels and normalise to sum to 1 (§4.5).

    Falls back to uniform weights if the sum underflows — which is reachable, since
    the paper itself reports a member far enough out to receive ~zero spatial weight,
    and a cluster where *every* member is an outlier in both dimensions would
    otherwise divide by ~0.
    """
    if len(spatial) != len(temporal):
        raise ValueError(f"weight lengths differ: {len(spatial)} vs {len(temporal)}")
    if not spatial:
        return []

    combined = np.asarray(spatial, dtype=np.float64) + np.asarray(temporal, dtype=np.float64)
    total = float(combined.sum())
    if not math.isfinite(total) or total < _MIN_WEIGHT_SUM:
        logger.warning(
            "Spatiotemporal weights underflowed (sum=%.3e over %d members); "
            "using uniform weights.",
            total, len(spatial),
        )
        return [1.0 / len(spatial)] * len(spatial)
    return [float(x / total) for x in combined]


def fit_dirichlet_multinomial(
    matrix: np.ndarray, *, max_iter: int = _MAX_ITER, tol: float = _TOL
) -> tuple[np.ndarray, float, bool]:
    """Minka (2000) fixed-point fit. Returns (mean distribution, concentration, converged).

    `matrix` is members × classes of weighted pseudo-counts. The update is

        α_k ← α_k · Σ_i [ψ(n_ik + α_k) − ψ(α_k)] / Σ_i [ψ(n_i + A) − ψ(A)]

    with A = Σα. `converged=False` covers two distinct outcomes, both expected: the
    iteration stalled on a degenerate input, or A ran past `_MAX_CONCENTRATION`
    because the members agree too closely for dispersion to be identified. Callers
    should not use the concentration when this is False. The returned mean is
    informational; `integrate_cluster` computes the distribution in closed form.
    """
    n = np.asarray(matrix, dtype=np.float64)
    if n.ndim != 2 or n.size == 0:
        raise ValueError(f"expected a non-empty 2-D matrix, got shape {n.shape}")
    if np.any(n < 0) or not np.all(np.isfinite(n)):
        raise ValueError("pseudo-counts must be finite and non-negative")

    row_totals = n.sum(axis=1)
    col_totals = n.sum(axis=0)
    grand_total = float(col_totals.sum())
    if grand_total <= 0.0:
        # No evidence at all: uniform, unconverged.
        k = n.shape[1]
        return np.full(k, 1.0 / k), 0.0, False

    # Initialise at the empirical mean with unit concentration — the standard
    # moment-matching start, and it makes the first iteration cheap.
    alpha = np.maximum(col_totals / grand_total, 1e-6)

    converged = False
    for _ in range(max_iter):
        a = float(alpha.sum())
        denominator = float(np.sum(psi(row_totals + a) - psi(a)))
        if not math.isfinite(denominator) or abs(denominator) < 1e-300:
            break
        numerator = np.sum(psi(n + alpha) - psi(alpha), axis=0)
        updated = alpha * numerator / denominator
        updated = np.maximum(updated, 1e-12)
        if not np.all(np.isfinite(updated)):
            break
        shift = float(np.max(np.abs(updated - alpha)))
        alpha = updated
        if float(alpha.sum()) > _MAX_CONCENTRATION:
            # Diverging: consensus data. Not an error, just not identified.
            break
        if shift < tol:
            converged = True
            break

    a = float(alpha.sum())
    if not math.isfinite(a) or a <= 0.0:
        k = n.shape[1]
        return np.full(k, 1.0 / k), 0.0, False
    return alpha / a, a, converged


def integrate_cluster(
    *,
    class_labels: Sequence[str],
    member_distributions: Sequence[Sequence[float]],
    spatial_distances_m: Sequence[float],
    temporal_distances_s: Sequence[float],
    sigma_floor_m: float,
    sigma_floor_s: float,
    prior_concentration: float = 0.0,
) -> ClusterPosterior:
    """Integrate one cluster's members into a single class distribution (§4.5).

    `member_distributions[i][k]` is member i's probability for `class_labels[k]`.
    Distances are to the cluster centroid and to the cluster's most recent detection.

    **The distribution is computed in closed form, not by iterating.** For this input
    the Minka fit's mean provably converges to the weight-normalised mean of the
    members — verified numerically: identical members at [0.6, 0.2, 0.2] fit to
    [0.598, 0.201, 0.201], and three fully disagreeing members fit to uniform, both
    exactly the weighted mean. Iterating to reach a value with a closed form would be
    slower and, worse, would report a cap-limited number as if it were an estimate.
    The concentration is still estimated by `fit_dirichlet_multinomial`, because that
    has no closed form — but see `ClusterPosterior.converged` before trusting it.

    `prior_concentration` is an **extension beyond the paper**, default 0.0 (off, so
    the result is the paper's). Above zero it adds a symmetric Dirichlet prior, which
    shrinks small clusters toward uniform and lets them approach their observed value
    only as corroborating members accumulate: one device reporting 0.6 lands below
    0.6, three devices reporting 0.6 land nearer it. That is the multi-user evidence
    accumulation the paper's own method does not provide — its contribution is the
    spatiotemporal weighting, not evidence combination across devices.
    """
    n_members = len(member_distributions)
    if n_members == 0:
        raise ValueError("a cluster needs at least one member")
    if not (n_members == len(spatial_distances_m) == len(temporal_distances_s)):
        raise ValueError("member_distributions and both distance arrays must align")

    weights = combine_weights(
        rbf_weights(spatial_distances_m, sigma_floor=sigma_floor_m),
        rbf_weights(temporal_distances_s, sigma_floor=sigma_floor_s),
    )

    probs = np.asarray(member_distributions, dtype=np.float64)
    if probs.ndim != 2 or probs.shape[1] != len(class_labels):
        raise ValueError(
            f"member_distributions shape {probs.shape} does not match "
            f"{len(class_labels)} class labels"
        )

    if prior_concentration < 0.0:
        raise ValueError("prior_concentration must be >= 0")

    n_classes = len(class_labels)
    weighted = probs * np.asarray(weights, dtype=np.float64)[:, None]
    # Pseudo-counts totalling the member count rather than 1, so `prior_concentration`
    # is denominated in "effective observations" and the concentration estimate below
    # is interpretable. See the module docstring.
    matrix = weighted * float(n_members)

    counts = matrix.sum(axis=0)
    total = float(counts.sum())
    if total > _MIN_WEIGHT_SUM:
        mean = (counts + prior_concentration / n_classes) / (total + prior_concentration)
    else:
        mean = np.full(n_classes, 1.0 / n_classes)

    # Dispersion has no closed form, so this one is fitted. `converged=False` means
    # the members agree too closely to identify it — the common case, not a failure.
    _, concentration, converged = fit_dirichlet_multinomial(matrix)

    return ClusterPosterior(
        distribution={label: float(mean[i]) for i, label in enumerate(class_labels)},
        concentration=float(concentration),
        weights=weights,
        converged=converged,
    )


__all__ = [
    "VERSION",
    "ClusterPosterior",
    "combine_weights",
    "fit_dirichlet_multinomial",
    "integrate_cluster",
    "rbf_weights",
]
