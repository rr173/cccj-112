"""Command line entry point for offline PCAP response recovery.

Example::

    python -m pcap_recover.cli \\
        --pcap capture.pcap --endpoints endpoints.json --output ./recovered
"""

import argparse
import json
import sys

from .errors import (
    ConfigError,
    FatalInputError,
    LimitExceeded,
    PcapRecoverError,
)
from .extract import _DEFAULT_LIMITS, run_recovery


def build_parser():
    parser = argparse.ArgumentParser(
        prog="pcap_recover",
        description=(
            "Reconstruct server-to-client HTTP/1.1 response bodies from a "
            "classic microsecond PCAP without any network access."
        ),
    )
    parser.add_argument("--pcap", required=True, help="input classic PCAP")
    parser.add_argument(
        "--endpoints",
        required=True,
        help="JSON config naming the server (ip, port) endpoints to track",
    )
    parser.add_argument(
        "--output",
        required=True,
        help="new output directory; it must not already exist",
    )
    parser.add_argument(
        "--max-connections",
        type=int,
        default=_DEFAULT_LIMITS["max_connections"],
        help="maximum number of distinct selected connections",
    )
    parser.add_argument(
        "--max-stream-bytes",
        type=int,
        default=_DEFAULT_LIMITS["max_stream_bytes"],
        help="maximum reconstructed TCP stream length per connection",
    )
    parser.add_argument(
        "--max-header-bytes",
        type=int,
        default=_DEFAULT_LIMITS["max_header_bytes"],
        help="maximum HTTP header section size per response",
    )
    parser.add_argument(
        "--max-decoded-bytes",
        type=int,
        default=_DEFAULT_LIMITS["max_decoded_bytes"],
        help="maximum decoded (post-gzip) body size per response",
    )
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    for name, value in (
        ("max-connections", args.max_connections),
        ("max-stream-bytes", args.max_stream_bytes),
        ("max-header-bytes", args.max_header_bytes),
        ("max-decoded-bytes", args.max_decoded_bytes),
    ):
        if value <= 0:
            print(f"pcap_recover: --{name} must be positive", file=sys.stderr)
            return 2

    limits = {
        "max_connections": args.max_connections,
        "max_stream_bytes": args.max_stream_bytes,
        "max_header_bytes": args.max_header_bytes,
        "max_decoded_bytes": args.max_decoded_bytes,
    }
    try:
        index = run_recovery(args.pcap, args.endpoints, args.output, limits)
    except FatalInputError as exc:
        print(
            f"pcap_recover: input structurally damaged ({exc.reason}): "
            f"{exc.detail}",
            file=sys.stderr,
        )
        return 3
    except ConfigError as exc:
        print(f"pcap_recover: configuration error: {exc}", file=sys.stderr)
        return 2
    except LimitExceeded as exc:
        print(f"pcap_recover: limit exceeded: {exc}", file=sys.stderr)
        return 2
    except (OSError, PcapRecoverError) as exc:
        print(f"pcap_recover: failed: {exc}", file=sys.stderr)
        return 2

    print(json.dumps(index["summary"], ensure_ascii=False, indent=2))
    failed = index["summary"]["failed_connections"]
    selected = index["summary"]["selected_connections"]
    if selected == 0:
        print(
            "pcap_recover: no traffic matched the configured endpoints",
            file=sys.stderr,
        )
        return 1
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
