"""Local telemetry -> analysis end-to-end test (real TCP, real SQLite)."""
import csv
import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.demo import run  # noqa: E402


def test_demo_passes_all_checks(tmp_path):
    demo = run(str(tmp_path / "demo"))
    assert all(c["ok"] for c in demo.checks)
    assert len(demo.checks) >= 40


def test_analysis_consumes_exported_observations():
    """Run the whole chain in a subprocess so global state cannot leak."""
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts",
                                      "run_telemetry_analysis.py")],
        cwd=ROOT, capture_output=True, text=True, timeout=180)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "reference check: 0 mismatches" in proc.stdout
    obs_csv = os.path.join(ROOT, "data", "obs", "station_obs_telemetry.csv")
    with open(obs_csv, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    assert [(r["station_id"], r["valid_time"], r["precip_mm"]) for r in rows] == [
        ("S1", "2026-02-01T03:00:00Z", "1.2"),
        ("S1", "2026-02-01T06:00:00Z", "1.8"),
        ("S2", "2026-02-01T03:00:00Z", "0.4"),
        ("S2", "2026-02-01T06:00:00Z", "0.6"),
        ("S3", "2026-02-01T03:00:00Z", "0"),
        ("S3", "2026-02-01T06:00:00Z", "0"),
        ("S4", "2026-02-01T03:00:00Z", "0.2"),
    ]
    outdir = [line.split("->", 1)[1].strip() for line in proc.stdout.splitlines()
              if line.startswith("analysis ->")][0]
    with open(os.path.join(outdir, "station_probs.csv"), newline="",
              encoding="utf-8") as f:
        sp = list(csv.DictReader(f))
    consumed = {(r["station_id"], r["valid_time"], r["obs_interval_mm"],
                 r["obs_version"]) for r in sp if r["obs_interval_mm"]}
    assert consumed == {
        ("S1", "2026-02-01T06:00:00Z", "1.8", "1"),
        ("S2", "2026-02-01T06:00:00Z", "0.6", "1"),
        ("S3", "2026-02-01T06:00:00Z", "0", "1"),
    }
    # S4 has no accepted data for 03-06 UTC: the cell stays empty
    s4 = [r for r in sp if r["station_id"] == "S4"
          and r["valid_time"] == "2026-02-01T06:00:00Z"]
    assert len(s4) == 3  # one row per configured threshold
    assert all(r["obs_interval_mm"] == "" and r["obs_event"] == ""
               and r["obs_version"] == "" for r in s4)
