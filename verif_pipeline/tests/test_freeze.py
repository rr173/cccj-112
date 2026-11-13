"""Frozen-analysis guarantees.

* Same frozen inputs -> identical snapshot id and identical outputs.
* Observation corrections issued AFTER the obs cutoff must not change
  an already frozen analysis (same snapshot id, same scores).
"""
import filecmp
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import fixture  # noqa: E402
from pipeline.runner import run_analysis  # noqa: E402

FROZEN_OUTPUTS = [
    "scores_threshold.csv",
    "scores_crps.csv",
    "reliability.csv",
    "station_probs.csv",
    "station_member.csv",
    "grid_intervals.csv",
    "obs_selected.csv",
    "selected_files.csv",
    "unused_files.csv",
    "accum_events.csv",
]


def test_snapshot_stable_for_identical_inputs():
    with tempfile.TemporaryDirectory() as tmp:
        out1 = run_analysis(fixture.build(os.path.join(tmp, "a")))
        out2 = run_analysis(fixture.build(os.path.join(tmp, "b")))
        assert os.path.basename(out1) == os.path.basename(out2)
        for name in FROZEN_OUTPUTS:
            assert filecmp.cmp(os.path.join(out1, name), os.path.join(out2, name),
                               shallow=False), name


def test_post_cutoff_corrections_do_not_change_frozen_analysis():
    post_cutoff_rows = [
        # correction of an existing observation, issued after the cutoff
        {"station_id": "A", "valid_time": "2026-01-01T06:00:00Z",
         "precip_mm": 99.0, "version": 2, "issued_time": "2026-01-05T00:00:00Z"},
        # a brand-new station/valid-time row, also after the cutoff
        {"station_id": "C", "valid_time": "2026-01-01T06:00:00Z",
         "precip_mm": 50.0, "version": 1, "issued_time": "2026-01-05T00:00:00Z"},
    ]
    with tempfile.TemporaryDirectory() as tmp:
        out1 = run_analysis(fixture.build(os.path.join(tmp, "a")))
        out2 = run_analysis(fixture.build(os.path.join(tmp, "b"),
                                          extra_obs=post_cutoff_rows))
        # the snapshot id is computed over frozen content, so it is identical
        assert os.path.basename(out1) == os.path.basename(out2)
        for name in FROZEN_OUTPUTS:
            assert filecmp.cmp(os.path.join(out1, name), os.path.join(out2, name),
                               shallow=False), name


if __name__ == "__main__":
    test_snapshot_stable_for_identical_inputs()
    print("test_snapshot_stable_for_identical_inputs OK")
    test_post_cutoff_corrections_do_not_change_frozen_analysis()
    print("test_post_cutoff_corrections_do_not_change_frozen_analysis OK")
