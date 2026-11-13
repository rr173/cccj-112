"""Accumulated precipitation -> interval precipitation.

Conventions (declared, do not change silently)
----------------------------------------------
For two consecutive lead times ``s < t`` (hours since cycle start) of one
cycle, with accumulation counter reset period ``R``
(``reset_interval_hours``, 0 disables resets):

* ``s == 0`` or ``s % R == 0``: the accumulation counter was (re)started
  at ``s``, so the interval amount is the accumulated value itself:
  ``interval[s, t] = accum(t)``. ``s == 0`` is the cycle start (flag
  ``start``); ``s > 0`` is a period reset (flag ``period_reset``).
* otherwise: ``interval[s, t] = accum(t) - accum(s)``.
* a difference ``< -neg_tol`` is an anomaly (flag
  ``negative_diff_anomaly``): the interval is set to NaN and the member
  is excluded from the valid-member denominator for that interval.
* a difference in ``[-neg_tol, 0)`` is treated as rounding noise and
  clipped to 0 (flag ``negative_diff_clipped``).

Missing accumulated values (NaN, e.g. decoded ``_FillValue`` /
``missing_value`` sentinels or non-finite cells) never take part in
arithmetic:

* a missing ``accum(t)`` makes ``interval[s, t]`` missing
  (flag ``missing_input``);
* a difference needs both endpoints: a missing ``accum(s)`` also makes
  ``interval[s, t]`` missing (flag ``missing_previous``);
* after an accumulation-counter reset the new segment starts from the
  new reading alone, so a valid ``accum(t)`` at/after a reset recovers
  the interval even if the previous segment ended in missing data.

Accumulated amounts are never interpreted as per-interval rates.
"""
from __future__ import annotations

import numpy as np

FLAG_OK = 0
FLAG_START = 1
FLAG_PERIOD_RESET = 2
FLAG_CLIPPED = 3
FLAG_ANOMALY = 4
FLAG_MISSING_INPUT = 5
FLAG_MISSING_PREV = 6

FLAG_NAMES = {
    FLAG_OK: "ok",
    FLAG_START: "start_of_cycle",
    FLAG_PERIOD_RESET: "period_reset",
    FLAG_CLIPPED: "negative_diff_clipped",
    FLAG_ANOMALY: "negative_diff_anomaly",
    FLAG_MISSING_INPUT: "missing_input",
    FLAG_MISSING_PREV: "missing_previous",
}


def compute_intervals(leads_hours, accum, reset_interval_hours, neg_tol=1e-6):
    """Difference accumulated precipitation into interval amounts.

    Parameters
    ----------
    leads_hours : sequence of int, length nt (ascending)
    accum : ndarray (nt, nlat, nlon), accumulated mm (NaN = missing)
    reset_interval_hours : int, accumulation reset period R (0 = never)
    neg_tol : float, tolerance for negative differences (mm)

    Returns
    -------
    intervals : ndarray like ``accum`` (NaN where anomalous or missing)
    flags : int8 ndarray like ``accum`` (see FLAG_* constants)
    """
    leads = [int(h) for h in leads_hours]
    if leads != sorted(leads):
        raise ValueError("lead times must be ascending")
    accum = np.asarray(accum, dtype=float)
    finite = np.isfinite(accum)
    intervals = np.full(accum.shape, np.nan)
    flags = np.full(accum.shape, FLAG_OK, dtype=np.int8)
    for i, _ in enumerate(leads):
        if i == 0:
            intervals[i] = accum[i]
            flags[i] = np.where(finite[i], FLAG_START, FLAG_MISSING_INPUT)
            continue
        s = leads[i - 1]
        if reset_interval_hours and s % reset_interval_hours == 0:
            # counter (re)started at s: the new segment needs only accum[i]
            intervals[i] = accum[i]
            flags[i] = np.where(finite[i], FLAG_PERIOD_RESET,
                                FLAG_MISSING_INPUT)
            continue
        diff = accum[i] - accum[i - 1]
        bad = diff < -neg_tol
        clip = (~bad) & (diff < 0.0)
        missing = ~(finite[i] & finite[i - 1])
        intervals[i] = np.where(missing | bad, np.nan,
                                np.where(clip, 0.0, diff))
        flags[i] = np.where(
            ~finite[i], FLAG_MISSING_INPUT,
            np.where(~finite[i - 1], FLAG_MISSING_PREV,
                     np.where(bad, FLAG_ANOMALY,
                              np.where(clip, FLAG_CLIPPED, FLAG_OK))),
        ).astype(np.int8)
    return intervals, flags
