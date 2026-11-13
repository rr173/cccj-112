"""Probabilistic scores. All conventions are declared in README.md.

* Threshold events use the ``>=`` convention.
* Brier score: weighted mean of (prob - obs)^2.
* Reliability bins: fixed edges 0, w, 2w, ..., 1.0 (w = bin width);
  bins are left-closed [lo, hi) except the top bin which is closed
  [1-w, 1.0]. Probabilities within 1e-9 of a lower edge are assigned to
  the bin starting at that edge (left-closed rule made robust to
  floating-point representation).
* Ensemble CRPS: energy form
      CRPS = (1/M) sum_i |x_i - y| - (1/(2 M^2)) sum_i sum_j |x_i - x_j|
  evaluated with the full pairwise sum. Tied member values need no
  tie-breaking: the pairwise sum is exact for any multiplicities.
* Empty groups return NaN (missing), never zero.
"""
from __future__ import annotations

import math

NAN = float("nan")


def weighted_mean(values, weights):
    """Weighted mean; NaN when there is nothing to average."""
    values = list(values)
    if not values:
        return NAN
    sw = sum(weights)
    if sw <= 0.0:
        return NAN
    return sum(v * w for v, w in zip(values, weights)) / sw


def brier_score(probs, obs, weights):
    """Weighted Brier score of probabilistic >= threshold forecasts."""
    probs = list(probs)
    if not probs:
        return NAN
    return weighted_mean([(p - o) ** 2 for p, o in zip(probs, obs)], weights)


def bin_index(p, width):
    """Reliability bin for probability p (left-closed, top bin closed)."""
    if not 0.0 <= p <= 1.0:
        raise ValueError(f"probability out of range: {p!r}")
    n_bins = int(round(1.0 / width))
    i = int(math.floor(p / width + 1e-9))
    return min(max(i, 0), n_bins - 1)


def reliability_table(probs, obs, weights, width):
    """One row per bin (all bins emitted, empty bins carry NaN stats)."""
    n_bins = int(round(1.0 / width))
    buckets = [[] for _ in range(n_bins)]
    for p, o, w in zip(probs, obs, weights):
        buckets[bin_index(p, width)].append((p, o, w))
    rows = []
    for i, cases in enumerate(buckets):
        row = {
            "bin_index": i,
            "bin_lo": i * width,
            "bin_hi": (i + 1) * width,
            "n_cases": len(cases),
            "weighted_n": 0.0,
            "mean_prob": NAN,
            "obs_freq": NAN,
        }
        if cases:
            ps = [c[0] for c in cases]
            os_ = [c[1] for c in cases]
            ws = [c[2] for c in cases]
            row["weighted_n"] = sum(ws)
            row["mean_prob"] = weighted_mean(ps, ws)
            row["obs_freq"] = weighted_mean(os_, ws)
        rows.append(row)
    return rows


def crps_ensemble(members, y):
    """Ensemble CRPS, energy form with the exact pairwise sum.

    ``members`` are the valid member values only (the denominator M is
    the number of valid members, not the configured ensemble size).
    """
    members = [float(m) for m in members]
    m = len(members)
    if m == 0:
        return NAN
    y = float(y)
    term1 = sum(abs(x - y) for x in members) / m
    term2 = 0.0
    for a in members:
        for b in members:
            term2 += abs(a - b)
    term2 /= 2.0 * m * m
    return term1 - term2
