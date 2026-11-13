"""Protocol validation and canonicalisation."""
import hashlib
import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.protocol import (ProtocolError, canonical_tip_mm,  # noqa: E402
                                empty_digest, format_ranges, missing_ranges,
                                normalize_message, tip_digest)


def _begin(**kw):
    base = {"type": "begin", "station_id": "S1", "batch_id": "b",
            "start_ms": 0, "end_ms": 3600000, "count": 2,
            "tip_mm": "0.2", "sha256": hashlib.sha256(b"").hexdigest()}
    base.update(kw)
    return base


def test_begin_round_and_range_checks():
    n = normalize_message(_begin())
    assert n["start_ms"] == 0 and n["end_ms"] == 3600000 and n["count"] == 2
    assert n["tip_mm"] == "0.2"
    for kw in ({"start_ms": 500}, {"end_ms": 3599999},
               {"start_ms": 3600000}, {"end_ms": 7200001},
               {"count": 100001}, {"count": -1},
               {"tip_mm": "0"}, {"tip_mm": "-0.2"}, {"tip_mm": 0.2},
               {"tip_mm": "0.20x"}, {"sha256": "xyz"},
               {"start_ms": True}):
        with pytest.raises(ProtocolError) as ei:
            normalize_message(_begin(**kw))
        assert ei.value.reason == "invalid_message"


def test_tip_field_validation():
    m = {"type": "tip", "station_id": "S1", "batch_id": "b",
         "seq": 0, "time_ms": 1000, "tips": 1}
    assert normalize_message(m)["tips"] == 1
    for kw in ({"tips": 0}, {"tips": 65536}, {"seq": -1},
               {"time_ms": 1.5}, {"tips": True}):
        with pytest.raises(ProtocolError):
            normalize_message({**m, **kw})


def test_unknown_type_and_bad_identity():
    with pytest.raises(ProtocolError):
        normalize_message({"type": "hello", "station_id": "S1", "batch_id": "b"})
    with pytest.raises(ProtocolError):
        normalize_message({"type": "seal", "station_id": "", "batch_id": "b"})
    with pytest.raises(ProtocolError):
        normalize_message({"type": "seal", "station_id": "a" * 129,
                           "batch_id": "b"})


def test_tip_mm_canonicalisation():
    assert canonical_tip_mm("0.20") == "0.2"
    assert canonical_tip_mm("0.200") == "0.2"
    assert canonical_tip_mm("1.0") == "1"
    assert canonical_tip_mm("0.001") == "0.001"
    assert canonical_tip_mm("12") == "12"
    for bad in ("00.2", ".2", "1.", "+1", "1e-3", "-1", "0", "0.000"):
        with pytest.raises(ProtocolError):
            canonical_tip_mm(bad)


def test_tip_digest_matches_declared_recipe():
    tips = [(2, 5000, 3), (0, 1000, 1), (1, 3000, 2)]
    # reference: sort by seq, text "seq,time_ms,tips\n" ASCII, concatenate
    text = "0,1000,1\n" + "1,3000,2\n" + "2,5000,3\n"
    assert tip_digest(tips) == hashlib.sha256(text.encode("ascii")).hexdigest()
    assert empty_digest() == hashlib.sha256(b"").hexdigest()


def test_missing_ranges_compaction():
    assert missing_ranges([0, 1, 2], 3) == []
    assert missing_ranges([0, 2], 3) == [(1, 1)]
    assert missing_ranges([], 5) == [(0, 4)]
    assert missing_ranges([0, 1, 3, 4, 6], 8) == [(2, 2), (5, 5), (7, 7)]
    assert missing_ranges([5], 7) == [(0, 4), (6, 6)]
    assert format_ranges([(2, 2), (4, 6)]) == ["2", "4-6"]
