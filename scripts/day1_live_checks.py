"""Thin wrapper so `python -m scripts.day1_live_checks` still works.

The implementation lives in grace/rzp/day1.py so that `grace day1` works from an
installed package, where `scripts/` is not importable.
"""
from grace.rzp.day1 import main

if __name__ == "__main__":
    import sys

    raise SystemExit(main(sys.argv[1:]))
