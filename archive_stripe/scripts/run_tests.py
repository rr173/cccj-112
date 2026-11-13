#!/usr/bin/env python3
"""Run the whole regression suite and print a concise summary.

Exit status is non-zero if any test failed.  This is the script the README
mentions; it delegates to ``unittest discovery`` so no third-party runner is
needed.
"""

import os
import sys
import unittest

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, os.path.join(_ROOT, "tests"))

if __name__ == "__main__":
    suite = unittest.defaultTestLoader.discover(
        os.path.join(_ROOT, "tests"), pattern="test_*.py"
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    print(
        f"\n{result.testsRun} tests, "
        f"{len(result.failures)} failures, {len(result.errors)} errors"
    )
    sys.exit(0 if result.wasSuccessful() else 1)
