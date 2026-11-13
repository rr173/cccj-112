#!/usr/bin/env python3
"""Local end-to-end telemetry integration demo.

Starts a real TCP server, drives it with a simulated field gateway (split
frames, glued frames, out-of-order and duplicate sends, bad CRC, garbage
bytes, a missing seq, a lost ACK and a restart, racing connections and
overlapping seals), then asserts the SQLite state and the exact exported
observation values.

Usage:
    python3 scripts/telemetry_demo.py        # writes runtime/demo/

The same scenarios run under pytest in tests/telemetry/.
Exit code 0 only if every check passes.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telemetry.demo import run

if __name__ == "__main__":
    run()
