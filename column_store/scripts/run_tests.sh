#!/usr/bin/env bash
# Full regression for column_store.  Standard library only (unittest), plus a
# separate naive row-at-a-time oracle and an LD_PRELOAD read monitor.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -B -m unittest discover -s tests -v
