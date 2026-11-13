"""Command-line interface: ``pack``, ``check`` and ``restore``.

Exit codes: 0 success, 2 usage / archive error (including a refused restore).
A failure never returns 0 and never overwrites an existing restore target.
"""

import argparse
import json
import os
import sys

from .check import check_pack, pack_source
from .errors import ArchiveError, ManifestError, RestoreRefused
from .manifest import K_MAX, K_MIN
from .restore import restore_pack


def _add_common(parser):
    parser.add_argument("pack_dir", help="pack directory")


def build_parser():
    parser = argparse.ArgumentParser(
        prog="archive_stripe",
        description=(
            "Two-fault-tolerant archival stripes for raw capture bytes: "
            "k data stripes plus P and Q over GF(256)."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("pack", help="encode a source file into a new pack")
    p.add_argument("source", help="source file (any bytes; never decoded as text)")
    p.add_argument("pack_dir", help="new or empty pack directory")
    p.add_argument("-k", "--data-stripes", type=int, required=True,
                   help=f"number of data stripes ({K_MIN}..{K_MAX})")
    p.add_argument("-B", "--block-size", type=int, required=True,
                   help="stripe block length in bytes")
    p.add_argument("--force", action="store_true",
                   help="replace an existing complete pack atomically")

    c = sub.add_parser("check", help="report health of every stripe")
    _add_common(c)
    c.add_argument("--json", action="store_true", help="machine-readable report")

    r = sub.add_parser("restore", help="rebuild the source from a pack")
    _add_common(r)
    r.add_argument("--output", required=True,
                   help="new file to write (existing files are never overwritten)")
    r.add_argument("--json", action="store_true", help="machine-readable result")
    return parser


def _print_report(report, as_json):
    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
        return
    print(f"pack: {report['pack_dir']}")
    if report["manifest"]["status"] != "ok":
        print(f"manifest: {report['manifest']['status']} "
              f"({report['manifest']['error']})")
        print("health: UNKNOWN (no valid manifest)")
        return
    print(f"manifest: ok  k={report['k']}  B={report['block_size']}  "
          f"source_length={report['source_length']}")
    print(f"source sha256: {report['source_sha256']}")
    print("stripes:")
    for row in report["stripes"]:
        print(
            f"  {row['name']:7s} {row['role']:6s} {row['status']:15s} "
            f"expected={row['expected_length']} "
            f"actual={row['actual_length']} "
            f"sha={row['actual_sha256'] or '-'}"
        )
    if report["extra_files"]:
        print(f"extra files (not part of the pack): {report['extra_files']}")
    print(f"trusted: {report['trusted']}")
    print(f"lost: {report['lost']}")
    if report["recoverable"]:
        print(
            f"health: {'HEALTHY' if report['healthy'] else 'DEGRADED'} "
            f"(recoverable; mode={report.get('recovery_mode')})"
        )
    else:
        print(f"health: UNRECOVERABLE ({report['reason']})")


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.command == "pack":
            manifest = pack_source(
                args.source, args.pack_dir, args.data_stripes, args.block_size,
                force=args.force,
            )
            print(
                f"packed {manifest.source_length} bytes into {args.pack_dir} "
                f"(k={manifest.k}, B={manifest.block_size}, "
                f"{manifest.stripe_count()} stripes)"
            )
            return 0

        if args.command == "check":
            report = check_pack(args.pack_dir)
            _print_report(report, args.json)
            # An archive that cannot be restored is a real health failure.
            status = 0 if report["recoverable"] else 2
            return status

        if args.command == "restore":
            result = restore_pack(args.pack_dir, args.output)
            if args.json:
                print(json.dumps(
                    {
                        "path": result.path,
                        "bytes_written": result.bytes_written,
                        "source_sha256": result.source_sha256,
                        "used_stripes": result.used_stripes,
                        "reconstructed_stripes": result.reconstructed_stripes,
                        "discarded_stripes": result.discarded_stripes,
                    },
                    indent=2, sort_keys=True,
                ))
            else:
                print(
                    f"restored {result.bytes_written} bytes to {result.path} "
                    f"(sha256 {result.source_sha256})"
                )
                if result.discarded_stripes:
                    print(
                        "discarded as untrustworthy: "
                        + ", ".join(result.discarded_stripes)
                    )
                if result.reconstructed_stripes:
                    print(
                        "rebuilt by parity: "
                        + ", ".join(result.reconstructed_stripes)
                    )
            return 0
    except (ArchiveError, OSError, ValueError) as exc:
        print(f"{args.command} failed: {exc}", file=sys.stderr)
        return 2
    raise AssertionError("unreachable")  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
