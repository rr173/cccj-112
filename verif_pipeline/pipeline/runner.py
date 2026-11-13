"""Production verification pipeline orchestration.

Reads a frozen analysis configuration, executes the full chain

    manifest -> forecast files -> accumulation differencing
    -> station interpolation -> observation freeze -> probabilities
    -> scores -> regional aggregation -> versioned snapshot output

and writes every intermediate artefact needed to trace any aggregated
number back to members, observations and interpolation weights.
"""
from __future__ import annotations

import csv
import json
import math
import os
from datetime import timedelta

from .accum import FLAG_ANOMALY, FLAG_CLIPPED, FLAG_NAMES, FLAG_PERIOD_RESET, compute_intervals
from .config import load_config
from .interpolate import bilinear_weights
from .io_forecast import read_forecast_nc, read_manifest, resolve_manifest_paths, select_files
from .io_obs import read_obs, read_stations, select_obs_versions
from .scores import brier_score, crps_ensemble, reliability_table
from .snapshot import sha256_file, snapshot_id
from .timeutil import format_time, parse_time

SCHEMA_VERSION = "1.0"
NAN = float("nan")


def _fmt(x):
    """Deterministic CSV formatting; NaN/None become empty fields."""
    if x is None:
        return ""
    if isinstance(x, float):
        if math.isnan(x):
            return ""
        return f"{x:.10g}"
    return str(x)


def _write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in rows:
            w.writerow({k: _fmt(r.get(k)) for k in fieldnames})


def run_analysis(config_path: str) -> str:
    """Run the pipeline; returns the versioned output directory."""
    cfg = load_config(config_path)

    # --- 1. manifest: cycle cutoff + duplicate selection ----------------
    manifest_rows = read_manifest(cfg.manifest_path)
    resolve_manifest_paths(manifest_rows, cfg.manifest_path)
    selected, unused = select_files(manifest_rows, cfg.init_cutoff)
    for key in sorted(selected):
        row = selected[key]
        if not os.path.exists(row["_abspath"]):
            unused.append({**row, "reason": "file_missing"})
    selected = {k: v for k, v in selected.items() if os.path.exists(v["_abspath"])}
    if not selected:
        raise ValueError("no forecast files selected")

    # --- 2. read forecasts, check manifest consistency ------------------
    fc = {}
    for key in sorted(selected):
        row = selected[key]
        d = read_forecast_nc(row["_abspath"])
        if d["cycle"] != row["cycle"] or d["member"] != row["member"]:
            raise ValueError(f"manifest/NetCDF mismatch for {row['path']}")
        fc[key] = d

    cycles = sorted({c for c, _ in fc})
    # Expected members: the product-wide member set (union over cycles).
    # A member without a selected file in a cycle counts as missing there.
    expected_members = sorted({m for _, m in fc})

    ref = next(iter(fc.values()))
    lats, lons, leads = ref["lats"], ref["lons"], ref["leads"]
    for key, d in fc.items():
        if d["lats"] != lats or d["lons"] != lons or d["leads"] != leads:
            raise ValueError(f"inconsistent grid/lead times in {key}")

    # --- 3. accumulation -> interval precipitation (grid level) ---------
    intervals = {}
    for key, d in fc.items():
        intervals[key] = compute_intervals(
            d["leads"], d["apcp"], d["reset_interval_hours"], cfg.neg_diff_tolerance
        )

    grid_rows = []
    event_rows = []
    for cycle, member in sorted(fc):
        d = fc[(cycle, member)]
        iv, flags = intervals[(cycle, member)]
        for ti, lead in enumerate(d["leads"]):
            vt = format_time(parse_time(cycle) + timedelta(hours=lead))
            for ilat, lat in enumerate(lats):
                for ilon, lon in enumerate(lons):
                    flag = int(flags[ti, ilat, ilon])
                    grid_rows.append({
                        "cycle": cycle, "member": member, "lead_hours": lead,
                        "valid_time": vt, "lat": lat, "lon": lon,
                        "accum_mm": float(d["apcp"][ti, ilat, ilon]),
                        "interval_mm": float(iv[ti, ilat, ilon]),
                        "flag": FLAG_NAMES[flag],
                    })
                    if flag in (FLAG_PERIOD_RESET, FLAG_CLIPPED, FLAG_ANOMALY):
                        event_rows.append({
                            "cycle": cycle, "member": member, "lead_hours": lead,
                            "valid_time": vt, "lat": lat, "lon": lon,
                            "kind": FLAG_NAMES[flag],
                            "accum_prev_mm": float(d["apcp"][ti - 1, ilat, ilon]) if ti > 0 else NAN,
                            "accum_curr_mm": float(d["apcp"][ti, ilat, ilon]),
                            "interval_mm": float(iv[ti, ilat, ilon]),
                        })

    # --- 4. stations and explicit interpolation weights -----------------
    stations = read_stations(cfg.stations_path)
    for st in stations:
        wts, clamped = bilinear_weights(lats, lons, st["lat"], st["lon"])
        st["interp"] = wts
        st["clamped"] = clamped
        st["interp_json"] = json.dumps(
            [{"lat": lats[i], "lon": lons[j], "weight": w} for i, j, w in wts],
            separators=(",", ":"),
        )

    # --- 5. station-level member intervals ------------------------------
    sm_rows = []
    case_values = {}  # (cycle, lead, station_id) -> list of (member, value|None)
    for cycle, member in sorted(fc):
        d = fc[(cycle, member)]
        iv, _ = intervals[(cycle, member)]
        for ti, lead in enumerate(d["leads"]):
            vt = format_time(parse_time(cycle) + timedelta(hours=lead))
            for st in stations:
                value = 0.0
                ok = True
                for i, j, w in st["interp"]:
                    x = float(iv[ti, i, j])
                    if math.isnan(x):
                        ok = False
                        break
                    value += w * x
                sm_rows.append({
                    "cycle": cycle, "member": member, "lead_hours": lead,
                    "valid_time": vt, "station_id": st["station_id"],
                    "interval_mm": value if ok else NAN, "valid": 1 if ok else 0,
                })
                key = (cycle, lead, st["station_id"])
                case_values.setdefault(key, []).append((member, value if ok else None))

    # --- 6. freeze observations -----------------------------------------
    obs_rows = read_obs(cfg.obs_path)
    obs_sel, obs_excluded = select_obs_versions(obs_rows, cfg.obs_cutoff)

    # --- 7. probabilities, events, per-case CRPS ------------------------
    sp_rows = []
    case_crps = {}  # (cycle, lead, station_id) -> crps | None
    for cycle in cycles:
        for lead in leads:
            vt = format_time(parse_time(cycle) + timedelta(hours=lead))
            for st in stations:
                sid = st["station_id"]
                pairs = case_values.get((cycle, lead, sid), [])
                vals = [v for _, v in pairs if v is not None]
                n_valid = len(vals)
                n_expected = len(expected_members)
                obs = obs_sel.get((sid, vt))
                obs_mm = obs["precip_mm"] if obs else None
                obs_ver = obs["version"] if obs else None
                case_crps[(cycle, lead, sid)] = (
                    crps_ensemble(vals, obs_mm)
                    if vals and obs_mm is not None else None
                )
                for thr in cfg.thresholds:
                    prob = (
                        sum(1 for v in vals if v >= thr) / n_valid
                        if n_valid else NAN
                    )
                    sp_rows.append({
                        "cycle": cycle, "lead_hours": lead, "valid_time": vt,
                        "station_id": sid, "region": st["region"],
                        "station_weight": st["weight"], "threshold_mm": float(thr),
                        "n_valid_members": n_valid, "n_expected_members": n_expected,
                        "member_coverage": n_valid / n_expected if n_expected else NAN,
                        "forecast_prob": prob,
                        "obs_interval_mm": obs_mm,
                        "obs_event": (1 if obs_mm >= thr else 0) if obs_mm is not None else None,
                        "obs_version": obs_ver,
                        "interp_weights": st["interp_json"],
                    })

    # --- 8. aggregation by cycle / lead / region / threshold ------------
    regions = sorted({st["region"] for st in stations})

    thr_groups = {}
    for r in sp_rows:
        if r["obs_event"] is None or math.isnan(r["forecast_prob"]):
            continue
        key = (r["cycle"], r["lead_hours"], r["region"], r["threshold_mm"])
        thr_groups.setdefault(key, []).append(r)

    score_rows = []
    rel_rows = []
    for cycle in cycles:
        for lead in leads:
            vt = format_time(parse_time(cycle) + timedelta(hours=lead))
            for region in regions:
                for thr in cfg.thresholds:
                    rs = thr_groups.get((cycle, lead, region, float(thr)), [])
                    if rs:
                        probs = [r["forecast_prob"] for r in rs]
                        obss = [r["obs_event"] for r in rs]
                        ws = [r["station_weight"] for r in rs]
                        bs = brier_score(probs, obss, ws)
                        cov = sum(r["member_coverage"] for r in rs) / len(rs)
                        n_cases = len(rs)
                    else:
                        probs, obss, ws = [], [], []
                        bs, cov, n_cases = NAN, NAN, 0
                    score_rows.append({
                        "cycle": cycle, "lead_hours": lead, "valid_time": vt,
                        "region": region, "threshold_mm": float(thr),
                        "brier_score": bs, "n_cases": n_cases,
                        "mean_member_coverage": cov,
                    })
                    for row in reliability_table(probs, obss, ws, cfg.reliability_bin_width):
                        rel_rows.append({
                            "cycle": cycle, "lead_hours": lead, "region": region,
                            "threshold_mm": float(thr), **row,
                        })

    crps_rows = []
    for cycle in cycles:
        for lead in leads:
            vt = format_time(parse_time(cycle) + timedelta(hours=lead))
            for region in regions:
                cases = []
                for st in stations:
                    if st["region"] != region:
                        continue
                    c = case_crps.get((cycle, lead, st["station_id"]))
                    if c is not None:
                        cases.append((c, st["weight"]))
                if cases:
                    sw = sum(w for _, w in cases)
                    crps_val = sum(c * w for c, w in cases) / sw
                    n_cases = len(cases)
                    covs = [
                        r["member_coverage"]
                        for r in sp_rows
                        if r["cycle"] == cycle and r["lead_hours"] == lead
                        and r["region"] == region and r["obs_event"] is not None
                        and not math.isnan(r["forecast_prob"])
                    ]
                    cov = sum(covs) / len(covs) if covs else NAN
                else:
                    crps_val, n_cases, cov = NAN, 0, NAN
                crps_rows.append({
                    "cycle": cycle, "lead_hours": lead, "valid_time": vt,
                    "region": region, "crps": crps_val, "n_cases": n_cases,
                    "mean_member_coverage": cov,
                })

    # --- 9. snapshot id over frozen content ------------------------------
    obs_frozen = ";".join(
        f"{k[0]}|{k[1]}|{v['precip_mm']!r}|{v['version']}|{v['issued_time']}"
        for k, v in sorted(obs_sel.items())
    )
    payload = {
        "schema": SCHEMA_VERSION,
        "config": cfg.canonical_dict(),
        "manifest_sha256": sha256_file(cfg.manifest_path),
        "stations_sha256": sha256_file(cfg.stations_path),
        "forecast_files": {
            selected[k]["path"]: sha256_file(selected[k]["_abspath"])
            for k in sorted(selected)
        },
        "obs_frozen": obs_frozen,
    }
    sid = snapshot_id(payload)
    outdir = os.path.join(cfg.output_root, f"{cfg.name}_{sid}")
    os.makedirs(outdir, exist_ok=True)

    # --- 10. write outputs ------------------------------------------------
    _write_csv(
        os.path.join(outdir, "selected_files.csv"),
        ("path", "cycle", "member", "priority", "run_id", "sha256"),
        [
            {**{k: selected[key][k] for k in ("path", "cycle", "member", "priority", "run_id")},
             "sha256": sha256_file(selected[key]["_abspath"])}
            for key in sorted(selected)
        ],
    )
    _write_csv(
        os.path.join(outdir, "unused_files.csv"),
        ("path", "cycle", "member", "priority", "run_id", "reason"),
        sorted(
            [{k: r.get(k) for k in ("path", "cycle", "member", "priority", "run_id", "reason")}
             for r in unused],
            key=lambda r: (r["cycle"], r["member"], r["priority"], r["path"]),
        ),
    )
    _write_csv(
        os.path.join(outdir, "grid_intervals.csv"),
        ("cycle", "member", "lead_hours", "valid_time", "lat", "lon",
         "accum_mm", "interval_mm", "flag"),
        grid_rows,
    )
    _write_csv(
        os.path.join(outdir, "accum_events.csv"),
        ("cycle", "member", "lead_hours", "valid_time", "lat", "lon", "kind",
         "accum_prev_mm", "accum_curr_mm", "interval_mm"),
        event_rows,
    )
    _write_csv(
        os.path.join(outdir, "station_member.csv"),
        ("cycle", "member", "lead_hours", "valid_time", "station_id",
         "interval_mm", "valid"),
        sm_rows,
    )
    _write_csv(
        os.path.join(outdir, "obs_selected.csv"),
        ("station_id", "valid_time", "precip_mm", "version", "issued_time"),
        [obs_sel[k] for k in sorted(obs_sel)],
    )
    _write_csv(
        os.path.join(outdir, "obs_excluded.csv"),
        ("station_id", "valid_time", "precip_mm", "version", "issued_time", "reason"),
        sorted(obs_excluded, key=lambda r: (r["station_id"], r["valid_time"], r["version"])),
    )
    _write_csv(
        os.path.join(outdir, "station_probs.csv"),
        ("cycle", "lead_hours", "valid_time", "station_id", "region",
         "station_weight", "threshold_mm", "n_valid_members", "n_expected_members",
         "member_coverage", "forecast_prob", "obs_interval_mm", "obs_event",
         "obs_version", "interp_weights"),
        sp_rows,
    )
    _write_csv(
        os.path.join(outdir, "scores_threshold.csv"),
        ("cycle", "lead_hours", "valid_time", "region", "threshold_mm",
         "brier_score", "n_cases", "mean_member_coverage"),
        score_rows,
    )
    _write_csv(
        os.path.join(outdir, "reliability.csv"),
        ("cycle", "lead_hours", "region", "threshold_mm", "bin_index",
         "bin_lo", "bin_hi", "n_cases", "weighted_n", "mean_prob", "obs_freq"),
        rel_rows,
    )
    _write_csv(
        os.path.join(outdir, "scores_crps.csv"),
        ("cycle", "lead_hours", "valid_time", "region", "crps", "n_cases",
         "mean_member_coverage"),
        crps_rows,
    )
    with open(os.path.join(outdir, "snapshot.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "snapshot_id": sid,
                "schema_version": SCHEMA_VERSION,
                "config": cfg.canonical_dict(),
                "inputs": {
                    "manifest_sha256": payload["manifest_sha256"],
                    "stations_sha256": payload["stations_sha256"],
                    "obs_file_sha256": sha256_file(cfg.obs_path),
                    "forecast_files": payload["forecast_files"],
                },
                "frozen": {
                    "obs_rows_used": len(obs_sel),
                    "obs_rows_excluded": len(obs_excluded),
                    "forecast_files_used": len(selected),
                    "forecast_files_unused": len(unused),
                },
            },
            f, indent=2, sort_keys=True,
        )
        f.write("\n")
    return outdir
