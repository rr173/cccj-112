"""Analysis configuration.

An analysis is frozen by two cutoffs declared here:
* ``init_cutoff``: only forecast cycles with cycle time <= this are analysed.
* ``obs_cutoff``: only observation versions issued <= this are used.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass


@dataclass
class AnalysisConfig:
    name: str
    manifest_path: str            # forecast product manifest (CSV)
    obs_path: str                 # station observations with versions (CSV)
    stations_path: str            # station metadata incl. region weights (CSV)
    output_root: str              # directory for versioned output snapshots
    init_cutoff: str              # ISO-8601; cycles <= this are included
    obs_cutoff: str               # ISO-8601; obs versions issued <= this are used
    thresholds: list              # mm; event := interval precipitation >= threshold
    reliability_bin_width: float = 0.1
    neg_diff_tolerance: float = 1e-6  # mm; |negative diff| below this is clipped to 0

    def canonical_dict(self) -> dict:
        """Content that influences numerical results (paths excluded)."""
        return {
            "name": self.name,
            "init_cutoff": self.init_cutoff,
            "obs_cutoff": self.obs_cutoff,
            "thresholds": [float(t) for t in self.thresholds],
            "reliability_bin_width": float(self.reliability_bin_width),
            "neg_diff_tolerance": float(self.neg_diff_tolerance),
        }


def load_config(path: str) -> AnalysisConfig:
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    cfg = AnalysisConfig(**raw)
    base = os.path.dirname(os.path.abspath(path))
    for key in ("manifest_path", "obs_path", "stations_path", "output_root"):
        value = getattr(cfg, key)
        if not os.path.isabs(value):
            setattr(cfg, key, os.path.normpath(os.path.join(base, value)))
    return cfg
