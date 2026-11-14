#!/usr/bin/env bash
# Reproduce the two-connection offline recovery demo without any network.
#
# Steps:
#   1. synthesize the capture (chunked+gzip vs. conflicting retransmit)
#   2. run the offline recovery command
#   3. independently verify the published bytes, digests and failure index
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(dirname "${HERE}")"
DATA="${HERE}/data"
OUT="${HERE}/recovered"
export PYTHONPATH="${ROOT}${PYTHONPATH:+:${PYTHONPATH}}"

rm -rf "${DATA}" "${OUT}"
mkdir -p "${DATA}"

python3 -P "${HERE}/build_demo_capture.py" "${DATA}"

set +e
python3 -P -m pcap_recover.cli \
    --pcap "${DATA}/two_downloads.pcap" \
    --endpoints "${DATA}/endpoints.json" \
    --output "${OUT}"
EXIT_CODE=$?
set -e

echo "recovery command exit code: ${EXIT_CODE}"
test "${EXIT_CODE}" -eq 1   # one selected connection failed -> non-zero

python3 -P "${HERE}/verify_demo.py" "${OUT}" "${DATA}/expected.json"
echo "demo artifacts: ${OUT}"
