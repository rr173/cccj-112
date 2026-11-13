"""Wire framing: split reads, glued frames, CRC/length recovery, bounds."""
import json
import os
import random
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from telemetry.frame import (MAX_BODY, MAX_FRAME, FrameDecoder,  # noqa: E402
                             encode_frame)


def msgs(n=5):
    return [{"type": "tip", "station_id": "S1", "batch_id": "b", "seq": i,
             "time_ms": 1000 * i, "tips": i + 1} for i in range(n)]


def test_roundtrip_single_frames():
    for m in msgs():
        dec = FrameDecoder()
        events = dec.feed(encode_frame(m))
        assert [e["type"] for e in events] == ["frame"]
        assert events[0]["message"] == m
        assert dec.pending_bytes == 0


def test_frame_split_at_every_byte_boundary():
    raw = b"".join(encode_frame(m) for m in msgs(3))
    for cut in range(1, len(raw)):
        dec = FrameDecoder()
        events = dec.feed(raw[:cut])
        events += dec.feed(raw[cut:])
        frames = [e["message"] for e in events if e["type"] == "frame"]
        assert frames == msgs(3), f"split at {cut} lost a frame"
        assert dec.pending_bytes == 0


def test_random_chunking_glues_frames():
    raw = b"".join(encode_frame(m) for m in msgs(20))
    rng = random.Random(7)
    dec = FrameDecoder()
    frames, pos = [], 0
    while pos < len(raw):
        n = rng.randint(1, 40)
        frames += [e["message"] for e in dec.feed(raw[pos:pos + n])
                   if e["type"] == "frame"]
        pos += n
    assert frames == msgs(20)


def test_truncated_frame_not_emitted():
    raw = encode_frame(msgs()[0])
    dec = FrameDecoder()
    events = dec.feed(raw[:-1])
    assert not any(e["type"] == "frame" for e in events)
    assert dec.pending_bytes == len(raw) - 1
    events = dec.feed(raw[-1:])
    assert [e["type"] for e in events] == ["frame"]


def test_bad_crc_recovers_to_next_frame():
    good = encode_frame(msgs()[1])
    bad = bytearray(encode_frame(msgs()[0]))
    bad[-2] ^= 0x01
    dec = FrameDecoder()
    events = dec.feed(bytes(bad) + good)
    reasons = [e["reason"] for e in events if e["type"] == "reject"]
    assert "bad_crc" in reasons
    frames = [e["message"] for e in events if e["type"] == "frame"]
    assert frames == [msgs()[1]]


def test_invalid_length_recovers():
    good = encode_frame(msgs()[2])
    dec = FrameDecoder()
    for length in (0, MAX_BODY + 1, 65535):
        header = b"\xa5\x5a" + length.to_bytes(2, "big")
        events = dec.feed(header + good)
        reasons = [e["reason"] for e in events if e["type"] == "reject"]
        assert "invalid_length" in reasons
        assert [e["message"] for e in events if e["type"] == "frame"] == [msgs()[2]]


def test_garbage_between_frames_is_skipped():
    dec = FrameDecoder()
    events = dec.feed(b"hello\x00\xff\xa5world" + encode_frame(msgs()[0])
                      + b"\x01\x02\x03" + encode_frame(msgs()[1]))
    frames = [e["message"]["seq"] for e in events if e["type"] == "frame"]
    assert frames == [0, 1]
    assert dec.pending_bytes == 0


def test_trailing_a5_kept_for_magic_split_across_reads():
    dec = FrameDecoder()
    events = dec.feed(b"junk\xa5")
    assert not any(e["type"] == "frame" for e in events)
    raw = encode_frame(msgs()[0])
    events = dec.feed(raw[1:])  # the earlier A5 completes the magic
    assert [e["message"] for e in events if e["type"] == "frame"] == [msgs()[0]]


def test_bad_json_rejected_without_desync():
    body = b"{not json"
    length = len(body).to_bytes(2, "big")
    import zlib
    crc = zlib.crc32(length + body) & 0xFFFFFFFF
    bogus = b"\xa5\x5a" + length + body + crc.to_bytes(4, "big")
    dec = FrameDecoder()
    events = dec.feed(bogus + encode_frame(msgs()[0]))
    assert "bad_json" in [e["reason"] for e in events if e["type"] == "reject"]
    assert [e["message"] for e in events if e["type"] == "frame"] == [msgs()[0]]


def test_unparsed_buffer_is_bounded():
    dec = FrameDecoder(max_buffer=8192)
    events = dec.feed(b"\x00" * 100000)
    assert dec.pending_bytes <= MAX_FRAME
    assert any(e["reason"] in ("garbage", "buffer_overflow")
               for e in events if e["type"] == "reject")


def test_body_length_limits_enforced_on_encode():
    with pytest.raises(ValueError):
        encode_frame({"a": "x" * MAX_BODY})
    with pytest.raises(ValueError):
        encode_frame({"a": "x" * (MAX_BODY - 2)})  # body > MAX_BODY


def test_frame_layout_is_the_declared_one():
    import zlib
    m = {"type": "seal", "station_id": "S1", "batch_id": "b"}
    raw = encode_frame(m)
    body = json.dumps(m, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode()
    assert raw[:2] == b"\xa5\x5a"
    assert int.from_bytes(raw[2:4], "big") == len(body) == 48
    assert raw[4:4 + len(body)] == body
    assert int.from_bytes(raw[-4:], "big") == zlib.crc32(raw[2:4] + body)
