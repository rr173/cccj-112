#!/usr/bin/env bash
# Full regression for the handover left-join job. Standard library only
# (this environment has no pytest); tests use unittest + an independent
# naive nested-loop oracle.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -I -m unittest discover -s tests -v
