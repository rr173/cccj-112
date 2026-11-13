"""Independent NumPy reference implementation of the verification chain.

This module shares no code with the production ``pipeline`` package. It
recomputes every published score directly from the raw inputs (manifest,
NetCDF forecasts, station/observation CSVs) with straightforward direct
formulas, then compares the results against the production output
directory. It exists to cross-check the production path.

Deliberately independent choices (same declared conventions, different
implementation):
* ensemble CRPS via the sorted-member identity
      CRPS = (1/M) sum_i |x_i - y| - (1/M^2) sum_i (2i - M - 1) x_(i)
  (exact for ties, since tied values carry adjacent coefficients)
  instead of the production pairwise double sum;
* the expected member set is derived from the manifest alone (eligible
  rows, cycle <= init_cutoff), never from the files that happen to be
  readable, so a shared wrong denominator cannot pass silently;
* ``_FillValue`` / ``missing_value`` / non-finite sentinels and
  ``scale_factor`` / ``add_offset`` packing are decoded here with
  separate code, so a placeholder scored as data is caught;
* plain Python loops for differencing, interpolation and aggregation.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import datetime, timedelta, timezone

import netCDF4
import numpy as np

NAN = float("nan")


# ---------------------------------------------------------------- raw input
def _parse(s):
    dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _fmt(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_inputs(config_path):
    with open(config_path, encoding="utf-8") as f:
        cfg = json.load(f)
    base = os.path.dirname(os.path.abspath(config_path))

    def res(p):
        return p if os.path.isabs(p) else os.path.normpath(os.path.join(base, p))

    cfg["manifest_path"] = res(cfg["manifest_path"])
    cfg["obs_path"] = res(cfg["obs_path"])
    cfg["stations_path"] = res(cfg["stations_path"])
    return cfg


def _select_files(cfg):
    cutoff = _parse(cfg["init_cutoff"])
    mdir = os.path.dirname(cfg["manifest_path"])
    cands = {}
    with open(cfg["manifest_path"], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if _parse(r["cycle"]) > cutoff:
                continue
            p = r["path"]
            ap = p if os.path.isabs(p) else os.path.normpath(os.path.join(mdir, p))
            if not os.path.exists(ap):
                continue
            key = (r["cycle"], int(r["member"]))
            cands.setdefault(key, []).append((int(r["priority"]), p, ap))
    # lowest priority value, then lexicographically smallest path
    return {k: sorted(v)[0][2] for k, v in cands.items()}


def _expected_members(cfg):
    """Declared member set: union of ``member`` over manifest rows whose
    cycle is at or before ``init_cutoff``. File existence and data
    quality are irrelevant here -- the expected set is fixed by the
    manifest alone (derived independently of the production code)."""
    cutoff = _parse(cfg["init_cutoff"])
    members = set()
    with open(cfg["manifest_path"], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if _parse(r["cycle"]) <= cutoff:
                members.add(int(r["member"]))
    return sorted(members)


def _read_nc(path):
    ds = netCDF4.Dataset(path)
    try:
        ds.set_auto_mask(False)
        ds.set_auto_scale(False)
        var = ds.variables["apcp"]
        stored = np.array(var[:], dtype=np.float64)
        names = set(var.ncattrs())
        lost = np.isinf(stored) | np.isnan(stored)
        for att in ("_FillValue", "missing_value"):
            if att in names:
                for sentinel in np.atleast_1d(
                        np.asarray(var.getncattr(att), dtype=np.float64)):
                    lost |= stored == float(sentinel)
        factor = float(var.getncattr("scale_factor")) \
            if "scale_factor" in names else 1.0
        shift = float(var.getncattr("add_offset")) \
            if "add_offset" in names else 0.0
        apcp = stored * factor + shift
        apcp[lost] = np.nan
        apcp[~np.isfinite(apcp)] = np.nan
        out = {
            "leads": [int(x) for x in ds.variables["lead_hours"][:]],
            "lats": [float(x) for x in ds.variables["lat"][:]],
            "lons": [float(x) for x in ds.variables["lon"][:]],
            "apcp": apcp,
            "reset": int(ds.getncattr("reset_interval_hours")),
        }
    finally:
        ds.close()
    return out


def _intervals(leads, apcp, reset, neg_tol):
    """Accumulated -> interval precipitation (declared reset rules)."""
    out = np.full(apcp.shape, np.nan)
    for i in range(apcp.shape[0]):
        if i == 0:
            out[i] = apcp[i]
            continue
        s = leads[i - 1]
        if reset and s % reset == 0:
            out[i] = apcp[i]  # counter restarted at s
            continue
        d = apcp[i] - apcp[i - 1]
        out[i] = np.where(d < -neg_tol, np.nan, np.maximum(d, 0.0))
    return out


def _weights(lats, lons, la, lo):
    def bracket(grid, x):
        n = len(grid)
        if n == 1 or x <= grid[0]:
            return 0, 0, 0.0
        if x >= grid[-1]:
            return n - 1, n - 1, 0.0
        for i in range(n - 1):
            if grid[i] <= x <= grid[i + 1]:
                return i, i + 1, (x - grid[i]) / (grid[i + 1] - grid[i])
        raise AssertionError

    i0, i1, fi = bracket(lats, la)
    j0, j1, fj = bracket(lons, lo)
    ws = []
    for i, wi in ((i0, 1.0 - fi), (i1, fi)):
        for j, wj in ((j0, 1.0 - fj), (j1, fj)):
            if wi * wj > 0.0:
                ws.append((i, j, wi * wj))
    return ws


def _select_obs(cfg):
    cutoff = _parse(cfg["obs_cutoff"])
    best = {}
    with open(cfg["obs_path"], newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if _parse(r["issued_time"]) > cutoff:
                continue
            key = (r["station_id"], r["valid_time"])
            cur = best.get(key)
            # highest version wins; exact (version, issued_time) ties are
            # broken by file order: the later row supersedes (declared
            # convention, same as the production stable-sort pick)
            if cur is None or (int(r["version"]), r["issued_time"]) >= (
                int(cur["version"]), cur["issued_time"]
            ):
                best[key] = r
    return best


# ------------------------------------------------------------------- scores
def _crps_sorted(members, y):
    """CRPS via the sorted-member identity (exact under ties)."""
    x = sorted(float(v) for v in members)
    m = len(x)
    term1 = sum(abs(v - y) for v in x) / m
    term2 = sum((2 * (i + 1) - m - 1) * x[i] for i in range(m)) / (m * m)
    return term1 - term2


def _bin_of(p, width):
    n_bins = int(round(1.0 / width))
    return min(max(int(math.floor(p / width + 1e-9)), 0), n_bins - 1)


def _wmean(pairs):
    pairs = list(pairs)
    if not pairs:
        return NAN
    sw = sum(w for _, w in pairs)
    return sum(v * w for v, w in pairs) / sw if sw > 0 else NAN


# -------------------------------------------------------------- computation
def _compute_reference(config_path):
    cfg = _load_inputs(config_path)
    thresholds = [float(t) for t in cfg["thresholds"]]
    binw = float(cfg.get("reliability_bin_width", 0.1))
    neg_tol = float(cfg.get("neg_diff_tolerance", 1e-6))

    files = _select_files(cfg)
    data = {}
    for key, path in sorted(files.items()):
        d = _read_nc(path)
        data[key] = {**d, "intervals": _intervals(d["leads"], d["apcp"], d["reset"], neg_tol)}
    if not data:
        raise ValueError("reference: no forecast files")

    cycles = sorted({c for c, _ in data})
    # Expected members: declared by the eligible manifest rows (cycle <=
    # init_cutoff), derived here independently of which files exist or
    # decode cleanly -- never from the readable files themselves.
    members_expected = _expected_members(cfg)
    any_d = next(iter(data.values()))
    leads, lats, lons = any_d["leads"], any_d["lats"], any_d["lons"]

    with open(cfg["stations_path"], newline="", encoding="utf-8") as f:
        stations = list(csv.DictReader(f))
    obs = _select_obs(cfg)

    station_probs = {}   # (cycle, lead, sid, thr) -> (prob, n_valid)
    case_valid = {}      # (cycle, lead, sid) -> n_valid (0 allowed)
    thr_groups = {}      # (cycle, lead, region, thr) -> [(prob, obs, w, cov)]
    crps_groups = {}     # (cycle, lead, region) -> [(crps, w, cov)]
    for cycle in cycles:
        for ti, lead in enumerate(leads):
            vt = _fmt(_parse(cycle) + timedelta(hours=lead))
            for st in stations:
                sid = st["station_id"]
                region = st["region"]
                w = float(st["weight"])
                ws = _weights(lats, lons, float(st["lat"]), float(st["lon"]))
                vals = []
                for m in members_expected:
                    d = data.get((cycle, m))
                    if d is None:
                        continue
                    iv = d["intervals"]
                    total, ok = 0.0, True
                    for i, j, wij in ws:
                        x = float(iv[ti, i, j])
                        if math.isnan(x):
                            ok = False
                            break
                        total += wij * x
                    if ok:
                        vals.append(total)
                case_valid[(cycle, lead, sid)] = len(vals)
                o = obs.get((sid, vt))
                obs_mm = float(o["precip_mm"]) if o else None
                cov = len(vals) / len(members_expected) if members_expected else NAN
                for thr in thresholds:
                    if not vals:
                        continue
                    p = sum(1 for v in vals if v >= thr) / len(vals)
                    station_probs[(cycle, lead, sid, thr)] = (p, len(vals))
                    if obs_mm is None:
                        continue
                    e = 1.0 if obs_mm >= thr else 0.0
                    thr_groups.setdefault((cycle, lead, region, thr), []).append(
                        (p, e, w, cov)
                    )
                if vals and obs_mm is not None:
                    crps_groups.setdefault((cycle, lead, region), []).append(
                        (_crps_sorted(vals, obs_mm), w, cov)
                    )

    return {
        "cycles": cycles,
        "leads": leads,
        "regions": sorted({st["region"] for st in stations}),
        "thresholds": thresholds,
        "binw": binw,
        "n_expected": len(members_expected),
        "station_probs": station_probs,
        "case_valid": case_valid,
        "thr_groups": thr_groups,
        "crps_groups": crps_groups,
    }


# --------------------------------------------------------------- comparison
def _read_production_csv(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _f(s):
    return float(s) if s not in ("", None) else NAN


def _close(a, b, tol):
    if math.isnan(a) and math.isnan(b):
        return True
    if math.isnan(a) or math.isnan(b):
        return False
    return abs(a - b) <= tol


def run_check(config_path, outdir, tol=1e-9, verbose=True):
    """Compare production output in ``outdir`` against this reference.

    Returns the number of mismatches (0 = production path verified).
    """
    ref = _compute_reference(config_path)
    mismatches = []

    def check(label, got, want):
        if not _close(got, want, tol):
            mismatches.append(f"{label}: production={got!r} reference={want!r}")

    # --- Brier score groups
    for row in _read_production_csv(os.path.join(outdir, "scores_threshold.csv")):
        key = (row["cycle"], int(row["lead_hours"]), row["region"], _f(row["threshold_mm"]))
        cases = ref["thr_groups"].get(key, [])
        want_bs = _wmean([((p - o) ** 2, w) for p, o, w, _ in cases])
        check(f"brier {key}", _f(row["brier_score"]), want_bs)
        check(f"brier n_cases {key}", _f(row["n_cases"]), float(len(cases)))
        want_cov = _wmean([(c, 1.0) for _, _, _, c in cases])
        check(f"brier coverage {key}", _f(row["mean_member_coverage"]), want_cov)

    # --- reliability bins
    for row in _read_production_csv(os.path.join(outdir, "reliability.csv")):
        key = (row["cycle"], int(row["lead_hours"]), row["region"], _f(row["threshold_mm"]))
        b = int(row["bin_index"])
        cases = [c for c in ref["thr_groups"].get(key, []) if _bin_of(c[0], ref["binw"]) == b]
        check(f"rel n {key} bin{b}", _f(row["n_cases"]), float(len(cases)))
        check(f"rel weighted_n {key} bin{b}", _f(row["weighted_n"]),
              sum(c[2] for c in cases) if cases else 0.0)
        check(f"rel mean_prob {key} bin{b}", _f(row["mean_prob"]),
              _wmean([(c[0], c[2]) for c in cases]))
        check(f"rel obs_freq {key} bin{b}", _f(row["obs_freq"]),
              _wmean([(c[1], c[2]) for c in cases]))

    # --- CRPS groups
    for row in _read_production_csv(os.path.join(outdir, "scores_crps.csv")):
        key = (row["cycle"], int(row["lead_hours"]), row["region"])
        cases = ref["crps_groups"].get(key, [])
        check(f"crps {key}", _f(row["crps"]), _wmean([(c, w) for c, w, _ in cases]))
        check(f"crps n_cases {key}", _f(row["n_cases"]), float(len(cases)))

    # --- station-level probabilities and member denominators
    # Every production row is checked against the independently derived
    # valid count, expected-member set and coverage -- including rows
    # with zero valid members, so a sentinel scored as data or an
    # expected set silently shrunk to the readable files is caught here.
    denom_checked = set()
    for row in _read_production_csv(os.path.join(outdir, "station_probs.csv")):
        ckey = (row["cycle"], int(row["lead_hours"]), row["station_id"])
        key = ckey + (_f(row["threshold_mm"]),)
        got_p = _f(row["forecast_prob"])
        want = ref["station_probs"].get(key)
        if want is None:
            if not math.isnan(got_p):
                mismatches.append(f"station_prob {key}: production={got_p!r} reference=missing")
        else:
            check(f"station_prob {key}", got_p, want[0])
        if ckey in denom_checked:
            continue
        denom_checked.add(ckey)
        want_n_valid = float(ref["case_valid"].get(ckey, 0))
        check(f"station_n_valid {ckey}", _f(row["n_valid_members"]), want_n_valid)
        check(f"station_n_expected {ckey}", _f(row["n_expected_members"]),
              float(ref["n_expected"]))
        want_cov = want_n_valid / ref["n_expected"] if ref["n_expected"] else NAN
        check(f"station_coverage {ckey}", _f(row["member_coverage"]), want_cov)

    if verbose:
        n_groups = (
            len(_read_production_csv(os.path.join(outdir, "scores_threshold.csv")))
            + len(_read_production_csv(os.path.join(outdir, "scores_crps.csv")))
        )
        if mismatches:
            print(f"REFERENCE CHECK FAILED: {len(mismatches)} mismatch(es)")
            for m in mismatches[:20]:
                print("  " + m)
        else:
            print(f"REFERENCE CHECK PASSED: {n_groups} score groups, "
                  f"{len(ref['station_probs'])} station probabilities, 0 mismatches")
    return len(mismatches)
