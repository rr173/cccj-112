"""End-to-end external left-join pipeline.

Stages (every stage streams; no stage materializes a whole input or a whole
key group in memory):

1. Each JSONL side is read line by line.  The composite key is extracted and
   encoded component-wise, and framed records are spilled into sorted runs via
   :class:`~handover_join.external.ExternalSorter` (chunk size tunable).
2. Runs are merged in bounded-fan-in waves into one sorted run per side.
3. The two sorted runs are merge-joined.  Equal-key groups are paired on disk:
   right-group boundaries are remembered as byte offsets and re-read per left
   row, so even a single hot key holding most of the data never materializes
   its m*n pairs or its n right rows in memory.
4. Pair frames are themselves externally sorted by (left_row, right_row),
   restoring the required output order without buffering the whole result.
"""

import json
import os
import struct
import tempfile

from .errors import InputDecodeError
from .external import ExternalSorter
from .frame import (
    MAGIC_PAIR,
    MAGIC_RECORD,
    FrameReader,
    decode_pair_body,
    decode_record_body,
    encode_pair_body,
    encode_record_body,
)
from .keycode import extract_key_for_row, validate_config

_ROW_KEY = struct.Struct(">QQ")  # (left_row, right_row-or-zero), 16 bytes


def _sort_side(
    input_path,
    components,
    side,
    work_dir,
    chunk_size,
    fan_in,
    chunk_bytes,
):
    """Spill one JSONL input into a single externally sorted run.

    Returns ``(sorted_run_path_or_None, sorter, row_count)``.
    """
    sorter = ExternalSorter(
        work_dir,
        prefix=side,
        chunk_size=chunk_size,
        fan_in=fan_in,
        chunk_bytes=chunk_bytes,
    )
    row_count = 0
    with open(input_path, "rb", buffering=1 << 16) as fh:
        for raw_line in fh:
            # A JSON object occupies one line; drop only the line terminator.
            if raw_line.endswith(b"\n"):
                raw_line = raw_line[:-1]
                if raw_line.endswith(b"\r"):
                    raw_line = raw_line[:-1]
            if not raw_line:
                # Blank lines are not JSON objects; the row counter still
                # advances so error locations match physical line numbers.
                raise InputDecodeError(side, row_count + 1, "empty line")
            row_count += 1
            try:
                obj = json.loads(raw_line)
            except json.JSONDecodeError as exc:
                raise InputDecodeError(side, row_count, str(exc)) from exc
            if not isinstance(obj, dict):
                raise InputDecodeError(
                    side, row_count, "top-level JSON value is not an object"
                )
            key = extract_key_for_row(obj, components, side, row_count)
            key_bytes = key if key is not None else b""
            # Re-serialize compactly for the run files.  Values, strings and
            # nested payloads survive byte-for-byte semantically (only member
            # order and insignificant whitespace change, which the contract
            # does not promise anyway), while the parsed object is released
            # immediately so a sort chunk holds bytes, not object graphs.
            payload = json.dumps(
                obj, ensure_ascii=False, separators=(",", ":")
            ).encode("utf-8")
            del obj
            sorter.add(
                MAGIC_RECORD, key_bytes, encode_record_body(row_count, payload)
            )
    run_path = sorter.finalize()
    return run_path, sorter, row_count


def _emit_pair(pair_sorter, left_row, right_row, left_payload, right_payload):
    sort_key = _ROW_KEY.pack(left_row, right_row or 0)
    body = encode_pair_body(left_row, right_row, left_payload, right_payload)
    pair_sorter.add(MAGIC_PAIR, sort_key, body)


def _join_sorted_runs(left_run, right_run, pair_sorter):
    """Merge-join two sorted side runs into the externally sorted pair spool.

    Returns the number of output pairs.  Right-only groups are skipped; left
    rows without a match are emitted once with a null right side.
    """
    pair_count = 0
    left_fh = open(left_run, "rb", buffering=1 << 16)
    right_fh = open(right_run, "rb", buffering=1 << 16) if right_run else None
    try:
        left_reader = FrameReader(left_fh)
        right_reader = FrameReader(right_fh) if right_fh else None
        lframe = left_reader.read_frame()
        rframe = right_reader.read_frame() if right_reader else None

        # Null keys (missing / null component) sort first with empty key bytes
        # and never match -- including both sides being null simultaneously.
        while lframe is not None and lframe[1] == b"":
            _row, payload = decode_record_body(lframe[2])
            _emit_pair(pair_sorter, _row, None, payload, None)
            pair_count += 1
            lframe = left_reader.read_frame()
        while rframe is not None and rframe[1] == b"":
            rframe = right_reader.read_frame()

        while lframe is not None:
            lkey = lframe[1]
            if rframe is None or lkey < rframe[1]:
                # Unmatched left row.
                row, payload = decode_record_body(lframe[2])
                _emit_pair(pair_sorter, row, None, payload, None)
                pair_count += 1
                lframe = left_reader.read_frame()
                continue
            if lkey > rframe[1]:
                # Right-only group: not part of a left join, skip it entirely.
                rkey = rframe[1]
                while rframe is not None and rframe[1] == rkey:
                    rframe = right_reader.read_frame()
                continue

            # Equal composite key: remember the right group's byte span and
            # re-read it from disk for each left row instead of buffering it.
            rkey = lkey
            group_start = rframe[3]
            while rframe is not None and rframe[1] == rkey:
                rframe = right_reader.read_frame()
            group_end = right_reader.tell()

            while lframe is not None and lframe[1] == rkey:
                left_row, left_payload = decode_record_body(lframe[2])
                right_reader.seek(group_start)
                while True:
                    scan = right_reader.read_frame()
                    if scan is None or scan[1] != rkey:
                        break
                    right_row, right_payload = decode_record_body(scan[2])
                    _emit_pair(
                        pair_sorter,
                        left_row,
                        right_row,
                        left_payload,
                        right_payload,
                    )
                    pair_count += 1
                right_reader.seek(group_end)
                lframe = left_reader.read_frame()
    finally:
        left_fh.close()
        if right_fh is not None:
            right_fh.close()
    return pair_count


def _write_output(pair_run, output_path):
    """Stream the sorted pair run out as one JSON object per line."""
    with open(pair_run, "rb", buffering=1 << 16) as fh, open(
        output_path, "wb", buffering=1 << 16
    ) as out:
        reader = FrameReader(fh)
        while True:
            frame = reader.read_frame()
            if frame is None:
                break
            magic, _key, body, _pos = frame
            if magic != MAGIC_PAIR:
                raise ValueError(f"unexpected frame magic {magic!r} in pair run")
            left_row, right_row, left_payload, right_payload = decode_pair_body(
                body
            )
            record = {
                "left_row": left_row,
                "right_row": right_row,
                "left": json.loads(left_payload),
                "right": json.loads(right_payload) if right_payload else None,
            }
            out.write(
                json.dumps(record, ensure_ascii=False, separators=(",", ":"))
                .encode("utf-8")
            )
            out.write(b"\n")


def run_join(
    left_input,
    right_input,
    config,
    output,
    work_dir=None,
    chunk_size=1000,
    fan_in=4,
    pair_chunk_size=None,
    chunk_bytes=None,
    pair_chunk_bytes=None,
):
    """Run the full external left join; return a statistics dictionary."""
    if chunk_size < 1:
        raise ValueError("chunk_size must be >= 1")
    if fan_in < 2:
        raise ValueError("fan_in must be >= 2")
    if chunk_bytes is not None and chunk_bytes < 1:
        raise ValueError("chunk_bytes must be >= 1 when provided")
    if pair_chunk_size is None:
        pair_chunk_size = chunk_size
    if pair_chunk_bytes is None:
        pair_chunk_bytes = chunk_bytes
    if pair_chunk_size < 1:
        raise ValueError("pair_chunk_size must be >= 1")
    if pair_chunk_bytes is not None and pair_chunk_bytes < 1:
        raise ValueError("pair_chunk_bytes must be >= 1 when provided")

    with open(config, "rb") as fh:
        config_obj = json.loads(fh.read().decode("utf-8"))
    components = validate_config(config_obj)

    if work_dir is not None:
        os.makedirs(work_dir, exist_ok=True)
    scratch = tempfile.mkdtemp(prefix="handover_join_", dir=work_dir)
    left_sorter = right_sorter = pair_sorter = None
    try:
        left_run, left_sorter, left_rows = _sort_side(
            left_input,
            components,
            "left",
            scratch,
            chunk_size,
            fan_in,
            chunk_bytes,
        )
        right_run, right_sorter, right_rows = _sort_side(
            right_input,
            components,
            "right",
            scratch,
            chunk_size,
            fan_in,
            chunk_bytes,
        )

        pair_sorter = ExternalSorter(
            scratch,
            prefix="pair",
            chunk_size=pair_chunk_size,
            fan_in=fan_in,
            chunk_bytes=pair_chunk_bytes,
        )
        if left_run is None:
            # Empty left table: the left join emits nothing at all, but the
            # output path always exists as a (zero-byte) JSONL file.
            pair_count = 0
            pair_run = None
            open(output, "wb").close()
        else:
            pair_count = _join_sorted_runs(left_run, right_run, pair_sorter)
            pair_run = pair_sorter.finalize()
            if pair_run is not None:
                _write_output(pair_run, output)
            else:
                open(output, "wb").close()
        return {
            "left_rows": left_rows,
            "right_rows": right_rows,
            "output_pairs": pair_count,
            "left_runs": left_sorter.initial_run_count if left_sorter else 0,
            "right_runs": right_sorter.initial_run_count if right_sorter else 0,
            "left_waves": getattr(left_sorter, "waves", 0),
            "right_waves": getattr(right_sorter, "waves", 0),
            "pair_waves": getattr(pair_sorter, "waves", 0),
            "work_dir": scratch,
            "chunk_size": chunk_size,
            "chunk_bytes": chunk_bytes,
            "pair_chunk_size": pair_chunk_size,
            "pair_chunk_bytes": pair_chunk_bytes,
            "fan_in": fan_in,
        }
    finally:
        for sorter in (left_sorter, right_sorter, pair_sorter):
            if sorter is not None:
                sorter.cleanup()
        # The scratch directory is always ours, whether or not its parent was
        # user-supplied; remove it once every run file has been cleaned up.
        try:
            os.rmdir(scratch)
        except OSError:
            pass
