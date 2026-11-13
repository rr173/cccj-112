"""Observation revisions only affect the valid times they pertain to.

A pre-cutoff correction of one (station, valid_time) value must change
exactly the score groups containing that valid time and leave all other
groups bit-identical.
"""
import csv
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from pipeline.runner import run_analysis  # noqa: E402

AFFECTED_LEAD = "12"  # the corrected valid time belongs to lead 12


def _by_key(path, keys):
    with open(path, newline="", encoding="utf-8") as f:
        return {tuple(r[k] for k in keys): r for r in csv.DictReader(f)}


def test_obs_revision_scope():
    revision = [
        {"station_id": "A", "valid_time": "2026-01-01T12:00:00Z",
         "precip_mm": 0.0, "version": 2, "issued_time": "2026-01-02T06:00:00Z"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out1 = run_analysis(fixture.build(os.path.join(tmp, "a")))
        out2 = run_analysis(fixture.build(os.path.join(tmp, "b"), extra_obs=revision))

        # different frozen content -> different snapshot id
        assert os.path.basename(out1) != os.path.basename(out2)

        keys = ("cycle", "lead_hours", "region", "threshold_mm")
        s1 = _by_key(os.path.join(out1, "scores_threshold.csv"), keys)
        s2 = _by_key(os.path.join(out2, "scores_threshold.csv"), keys)
        assert set(s1) == set(s2)
        for key in s1:
            _, lead, region, _ = key
            same = s1[key] == s2[key]
            if lead == AFFECTED_LEAD and region == "R1":
                assert not same, f"group {key} should have changed"
            else:
                assert same, f"group {key} must be untouched"

        keys = ("cycle", "lead_hours", "region")
        c1 = _by_key(os.path.join(out1, "scores_crps.csv"), keys)
        c2 = _by_key(os.path.join(out2, "scores_crps.csv"), keys)
        assert set(c1) == set(c2)
        for key in c1:
            _, lead, region = key
            same = c1[key] == c2[key]
            if lead == AFFECTED_LEAD and region == "R1":
                assert not same, f"CRPS group {key} should have changed"
            else:
                assert same, f"CRPS group {key} must be untouched"


if __name__ == "__main__":
    test_obs_revision_scope()
    print("test_obs_revision_scope OK")
