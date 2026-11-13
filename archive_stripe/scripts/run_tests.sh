#!/usr/bin/env bash
# Full regression for archive_stripe.  Standard library only: unittest with
# an independent reference codec as the oracle.  Ordinary tests use tiny
# fixtures; large-file work lives in scripts/memory_probe.py and
# scripts/stress_demo.py and is opt-in.
set -euo pipefail
cd "$(dirname "$0")/.."
python3 -B -m unittest discover -s tests -v
