#!/usr/bin/env bash
# Run the handover left-join example with deliberately small chunk and fan-in
# so the multi-wave external merge path is exercised on the tiny dataset.
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD"

python3 -B -E -m handover_join.cli \
  --left examples/handover/facts.jsonl \
  --right examples/handover/registry.jsonl \
  --config examples/handover/config.json \
  --output examples/handover/pairs.jsonl \
  --work-dir examples/handover/scratch \
  --chunk-size 2 \
  --pair-chunk-size 2 \
  --fan-in 2

echo "---- output ----"
cat examples/handover/pairs.jsonl
