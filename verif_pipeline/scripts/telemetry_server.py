#!/usr/bin/env python3
"""Start the local gauge telemetry receive service.

Usage:
    python3 scripts/telemetry_server.py [--db runtime/telemetry.sqlite3]
        [--stations data/obs/stations.csv] [--port 8765]
        [--journal runtime/gateway_journal.jsonl]

The database survives restarts: receiving simply continues where the
last committed transaction left off.  No broker, no remote account.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.server import main

if __name__ == "__main__":
    raise SystemExit(main())
