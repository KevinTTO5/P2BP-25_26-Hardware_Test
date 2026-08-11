"""`python -m mv3dt_installer` entrypoint (doc 00 §3.1's module layout).

Thin on purpose: all flag parsing, orchestration, and the dispatch loop
live in `app.py` (doc 00 §3.2 -- "`__main__.py` -> `app.main()`"). This
file's only job is to call it and forward the process exit code.
"""

from __future__ import annotations

import sys

from mv3dt_installer import app


def run() -> int:
    return app.main()


if __name__ == "__main__":
    sys.exit(run())
