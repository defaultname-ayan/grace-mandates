"""Day-1 live API checks (spec 17).

Run these BEFORE trusting anything the simulator says. Each check prints
PASS/FAIL/SKIP and appends to docs/WHAT-BROKE.md. A failure here is content for
the write-up, not a blocker -- the simulator covers what test mode cannot do.

Usage:  python -m scripts.day1_live_checks [--wait 300]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

LOG = Path.cwd() / "docs" / "WHAT-BROKE.md"


def log(line: str) -> None:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with LOG.open("a") as fh:
        fh.write(f"- `{stamp}` {line}\n")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="grace day1")
    ap.add_argument("--wait", type=int, default=0,
                    help="Seconds to wait for you to authorise the subscription in a browser.")
    ap.add_argument("--amount", type=int, default=49900)
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args(argv if argv is not None else [])

    print("Grace - Day-1 live checks against Razorpay TEST mode")
    print("=" * 60)

    if not os.getenv("RAZORPAY_KEY_ID"):
        print("\n[SKIP] RAZORPAY_KEY_ID not set.")
        print("       Copy .env.example to .env, put TEST-MODE keys in it, and export them.")
        print("       Everything else in Grace runs without this; only this script needs it.")
        log("Day-1 live checks SKIPPED: no RAZORPAY_KEY_ID in the environment.")
        return 0

    from grace.rzp.live import live_demo

    result = live_demo(amount_paise=args.amount, keep=args.keep, wait=args.wait)

    print("\n" + "=" * 60)
    passed = sum(1 for s in result.get("steps", []) if s["ok"])
    total = len(result.get("steps", []))
    print(f"{passed}/{total} checks passed")

    for s in result.get("steps", []):
        log(f"Day-1 check `{s['step']}`: {'PASS' if s['ok'] else 'FAIL'} - {s['detail']}")

    out = Path("runs") / "day1_live_checks.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, indent=2, default=str))
    print(f"Wrote {out}")

    print("\nStill-open questions this script answers when a subscription reaches `active`:")
    print("  1. Does resume keep the original cycle date or reschedule it?  -> charge_at_after_resume")
    print("  2. What value does pause_initiated_by take for a CUSTOMER pause? (undocumented)")
    print("  3. Is pause enabled by default on a fresh test account?")
    return 0


if __name__ == "__main__":
    sys.exit(main())
