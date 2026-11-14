"""``python3 -m column_store ...`` delegates to :mod:`column_store.cli`."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
