#!/usr/bin/env python3
"""Generate a larger, deterministic planning instance (offline).

The instance models six construction basins.  Each basin holds a 4x4
grid of candidate sites (16 candidates) and an 8x8 grid of river-reach
samples, all within one service radius, so the 16 sites in a basin
overlap heavily and form one coverage component.  The basins are far
apart (no sample is served by two basins), so the exact optimiser splits
the problem into six components and solves each exactly.

Total: 96 candidates, 384 samples, 4 existing stations.  Re-running
gives identical files (no randomness).
"""
import csv
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(ROOT, "data", "planning", "examples", "large")

# basin origins: far enough apart (> 2 * radius) that no sample crosses
BASINS = [(39.10, 115.10), (39.10, 117.90), (40.90, 115.10),
          (40.90, 117.90), (39.10, 119.10), (40.90, 119.10)]
RADIUS_KM = 25.0
CAND_STEP = 0.06      # degrees, ~6.7 km; 4x4 covers ~0.18 deg
SAMP_STEP = 0.02      # degrees, 8x8 covers ~0.14 deg
CAND_N, SAMP_N = 4, 8


def _write(path, fields, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields, lineterminator="\n")
        w.writeheader()
        w.writerows(rows)


def main():
    os.makedirs(OUT, exist_ok=True)
    stations = [
        {"station_id": "E1", "lat": 39.30, "lon": 115.30, "region": "West", "weight": 1.0},
        {"station_id": "E2", "lat": 41.10, "lon": 116.70, "region": "Mid", "weight": 1.0},
        {"station_id": "E3", "lat": 39.20, "lon": 119.00, "region": "East", "weight": 1.0},
        {"station_id": "E4", "lat": 41.00, "lon": 118.10, "region": "East", "weight": 1.0},
    ]
    samples, candidates = [], []
    for b, (lat0, lon0) in enumerate(BASINS):
        for r in range(SAMP_N):
            for c in range(SAMP_N):
                key = f"R{b}{r:02d}{c:02d}"
                weight = 1 + (r * SAMP_N + c + b) % 6     # 1..6
                if (r == 3 and c == 3):
                    weight += 12                          # one key reach per basin
                samples.append({
                    "sample_id": key,
                    "lat": f"{lat0 + 0.20 + (r - SAMP_N / 2) * SAMP_STEP:.4f}",
                    "lon": f"{lon0 + 0.20 + (c - SAMP_N / 2) * SAMP_STEP:.4f}",
                    "weight": weight,
                    "reach": f"basin {b} reach r{r} c{c}",
                })
        for r in range(CAND_N):
            for c in range(CAND_N):
                key = f"B{b}{r:02d}{c:02d}"
                cost = 4 + (r * CAND_N + c + b) % 9       # 4..12
                candidates.append({
                    "candidate_id": key,
                    "lat": f"{lat0 + 0.20 + (r - CAND_N / 2 + 0.5) * CAND_STEP:.4f}",
                    "lon": f"{lon0 + 0.20 + (c - CAND_N / 2 + 0.5) * CAND_STEP:.4f}",
                    "cost": cost,
                    "required": 1 if key == "B10200" else 0,
                    "forbidden": 1 if key == "B40203" else 0,
                    "name": f"basin {b} site r{r} c{c}",
                })
    _write(os.path.join(OUT, "stations.csv"),
           ("station_id", "lat", "lon", "region", "weight"), stations)
    _write(os.path.join(OUT, "samples.csv"),
           ("sample_id", "lat", "lon", "weight", "reach"), samples)
    _write(os.path.join(OUT, "candidates.csv"),
           ("candidate_id", "lat", "lon", "cost", "required", "forbidden", "name"),
           candidates)
    print(f"wrote {len(candidates)} candidates, {len(samples)} samples, "
          f"{len(stations)} existing stations to {OUT}")


if __name__ == "__main__":
    main()
