"""Independent verifier for the two-connection demonstration.

This script deliberately does not import any pcap_recover code except the
CLI's exit status convention: it reads ``index.json``, metadata files and
body files with plain file/JSON/hashlib calls and recomputes every length
and digest itself, then compares against the fixture's literal expectations.
"""

import hashlib
import json
import os
import sys


def fail(message):
    print(f"VERIFY FAILED: {message}", file=sys.stderr)
    return 1


def verify(output_dir, expected_path):
    with open(expected_path, encoding="utf-8") as handle:
        expected = json.load(handle)
    index_path = os.path.join(output_dir, "index.json")
    if not os.path.isfile(index_path):
        return fail(f"missing {index_path}")
    with open(index_path, encoding="utf-8") as handle:
        index = json.load(handle)

    summary = index["summary"]
    if summary["selected_connections"] != 2:
        return fail(
            f"expected 2 selected connections, got "
            f"{summary['selected_connections']}"
        )
    if summary["successful_connections"] != 1:
        return fail(
            f"expected 1 successful connection, got "
            f"{summary['successful_connections']}"
        )
    if summary["failed_connections"] != 1:
        return fail(
            f"expected 1 failed connection, got "
            f"{summary['failed_connections']}"
        )
    if summary["published_objects"] != expected[
        "expected_published_object_count"
    ]:
        return fail("published object count mismatch")

    ok_entries = [c for c in index["connections"] if c["status"] == "ok"]
    bad_entries = [c for c in index["connections"] if c["status"] == "failed"]
    if len(ok_entries) != 1 or len(bad_entries) != 1:
        return fail("connection status split is not 1 ok / 1 failed")

    bad = bad_entries[0]
    if bad["failure_reason"] != expected["connection_b_failure_reason"]:
        return fail(
            f"failure reason is {bad['failure_reason']!r}, expected "
            f"{expected['connection_b_failure_reason']!r}"
        )

    conn_dir = os.path.join(output_dir, ok_entries[0]["connection_dir"])
    if not os.path.isdir(conn_dir):
        return fail(f"missing connection directory {conn_dir}")
    # A failed connection must leave no directory of its own behind.
    listed = set(os.listdir(output_dir))
    if listed != {"index.json", ok_entries[0]["connection_dir"]}:
        return fail(f"unexpected output entries: {sorted(listed)}")

    for object_index in (1, 2):
        meta_path = os.path.join(conn_dir, f"metadata_{object_index}.json")
        body_path = os.path.join(conn_dir, f"body_{object_index}.bin")
        for path in (meta_path, body_path):
            if not os.path.isfile(path):
                return fail(f"missing published file {path}")
        with open(meta_path, encoding="utf-8") as handle:
            metadata = json.load(handle)
        with open(body_path, "rb") as handle:
            body = handle.read()
        recomputed = hashlib.sha256(body).hexdigest()
        if recomputed != metadata["body_sha256"]:
            return fail(
                f"body_{object_index}: digest {recomputed} disagrees with "
                f"metadata {metadata['body_sha256']}"
            )
        if recomputed != hashlib.sha256(body).hexdigest():
            return fail("unreachable")
        if len(body) != metadata["decoded_body_length_bytes"]:
            return fail(f"body_{object_index}: decoded length mismatch")
        # Offsets must point back into the declared stream window.
        offsets = metadata["stream_offsets"]
        if not (
            offsets["response_start"]
            <= offsets["header_end"]
            <= offsets["body_start"]
            <= offsets["body_end"]
        ):
            return fail(f"body_{object_index}: non-monotonic stream offsets")

    # Object 1: the gzip catalog.
    catalog_path = os.path.join(conn_dir, "body_1.bin")
    with open(catalog_path, "rb") as handle:
        catalog = handle.read()
    if hashlib.sha256(catalog).hexdigest() != expected["catalog_body"][
        "sha256_hex"
    ]:
        return fail("catalog body SHA-256 mismatch")
    if len(catalog) != expected["catalog_body"]["decoded_length"]:
        return fail("catalog body length mismatch")
    meta1 = json.load(
        open(os.path.join(conn_dir, "metadata_1.json"), encoding="utf-8")
    )
    if meta1["framing"] != "chunked":
        return fail("catalog response must be recorded as chunked")
    if meta1["content_encoding"] != "gzip":
        return fail("catalog response must record gzip encoding")
    # After de-chunking, the payload must be exactly the raw gzip member;
    # the on-wire body is larger because of chunk framing and the trailer.
    if meta1["raw_body_length_bytes"] != expected["catalog_body"][
        "gzip_wire_length"
    ]:
        return fail(
            "de-chunked payload length does not match the gzip member size"
        )
    if not (meta1["wire_body_length_bytes"] > meta1["raw_body_length_bytes"]):
        return fail("chunked wire body must be larger than its payload")

    # Object 2: the plain follow-up response, exact byte comparison.
    with open(os.path.join(conn_dir, "body_2.bin"), "rb") as handle:
        second = handle.read()
    if second.hex() != expected["second_body"]["exact_bytes_hex"]:
        return fail("second response body bytes differ from fixture")
    if len(second) != expected["second_body"]["length"]:
        return fail("second response body length mismatch")

    print("VERIFY OK")
    print(json.dumps({
        "successful_connection": ok_entries[0]["connection_dir"],
        "failed_connection_reason": bad["failure_reason"],
        "catalog_sha256": expected["catalog_body"]["sha256_hex"],
        "objects": summary["published_objects"],
    }, indent=2))
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("usage: verify_demo.py OUTPUT_DIR EXPECTED_JSON", file=sys.stderr)
        sys.exit(2)
    sys.exit(verify(sys.argv[1], sys.argv[2]))
