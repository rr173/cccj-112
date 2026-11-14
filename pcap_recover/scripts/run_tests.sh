#!/usr/bin/env bash
# Run the complete acceptance test suite using only the standard library.
# Tests are self-contained and generate every PCAP fixture they need.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "${HERE}")"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

cd "${ROOT}"
python3 -P -m unittest discover -s tests -v
