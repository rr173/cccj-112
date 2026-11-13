"""Explicit spatial interpolation weights: grid -> station.

Each station is mapped onto the surrounding grid points with bilinear
weights. The weights are stored with the verification output so every
station value can be traced back to its contributing grid points.

Declared behaviour: a station outside the grid domain is clamped to the
nearest edge (``clamped`` flag set); if any contributing grid point has
a NaN interval value, the station value is NaN (NaN propagates).
"""
from __future__ import annotations


def _bracket(grid, x):
    """Return (i0, i1, fraction, clamped) such that x lies in
    [grid[i0], grid[i1]] and fraction interpolates between them."""
    n = len(grid)
    if n == 1:
        return 0, 0, 0.0, x != grid[0]
    if x <= grid[0]:
        return 0, 0, 0.0, x < grid[0]
    if x >= grid[-1]:
        return n - 1, n - 1, 0.0, x > grid[-1]
    for i in range(n - 1):
        if grid[i] <= x <= grid[i + 1]:
            frac = (x - grid[i]) / (grid[i + 1] - grid[i])
            return i, i + 1, frac, False
    raise AssertionError("unreachable")


def bilinear_weights(lats, lons, lat_s, lon_s):
    """Bilinear weights for station (lat_s, lon_s) on a regular grid.

    Returns ``(weights, clamped)`` where ``weights`` is a sorted list of
    ``(ilat, ilon, weight)`` with strictly positive weights summing to 1.
    """
    i0, i1, fi, ci = _bracket(lats, lat_s)
    j0, j1, fj, cj = _bracket(lons, lon_s)
    weights = []
    for i, wi in ((i0, 1.0 - fi), (i1, fi)):
        for j, wj in ((j0, 1.0 - fj), (j1, fj)):
            w = wi * wj
            if w > 0.0:
                weights.append((i, j, w))
    weights.sort()
    return weights, bool(ci or cj)
