#!/usr/bin/env python3
"""Run the planner test suite without a pytest dependency.

The tests are ordinary functions named ``test_*`` in
``tests/planning/test_planning.py``; this runner imports the module,
calls every test function, and reports pass/fail with a non-zero exit
code on the first failure.  Using pytest is also supported and preferred
when it is installed:

    python3 -m pytest tests/planning -q
"""
from __future__ import annotations

import os
import sys
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tests", "planning"))
sys.path.insert(0, os.path.join(ROOT, "scripts", "planning"))

import test_planning  # noqa: E402


def main():
    tests = [(name, fn) for name, fn in sorted(vars(test_planning).items())
             if name.startswith("test_") and callable(fn)]
    passed = 0
    for name, fn in tests:
        try:
            fn()
        except Exception:
            print(f"FAIL {name}")
            traceback.print_exc()
            print(f"\n{passed} passed, {len(tests) - passed} failed")
            return 1
        passed += 1
        print(f"ok   {name}")
    print(f"\n{passed} passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
