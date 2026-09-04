"""Pick concrete mandate ids for the pitch video (they change with the seed)."""
from __future__ import annotations

import argparse
from pathlib import Path

from grace.orchestrator import arm_db_path
from grace.store import Store
from grace.util import fmt_inr


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", default="demo")
    args = ap.parse_args()
    rd = Path("runs") / args.run

    agent = Store(arm_db_path(rd, "agent"))
    base = Store(arm_db_path(rd, "baseline"))
    try:
        ad = agent.decisions_for_arm("agent")
        bd = base.decisions_for_arm("baseline")

        def truth_of(mid: str):
            return agent.get_truth(mid) or base.get_truth(mid)

        def show(title: str, rows: list[tuple[str, dict]], n: int = 3,
                 prefer_at_risk: bool = True) -> None:
            print(f"\n{title}")
            if not rows:
                print("  (none found - reseed or widen the filter)")
            if prefer_at_risk:
                # Put genuinely at-risk mandates first: a demo case should show
                # a real save, not an accidental false intervention.
                rows = sorted(rows, key=lambda kv: not (
                    (t := truth_of(kv[0])) is not None and t.will_fail))
            for mid, d in rows[:n]:
                t = truth_of(mid)
                tag = ""
                if t is not None:
                    tag = f"   [truth: {'AT RISK - ' + t.cause.value if t.will_fail else 'healthy'}]"
                m = agent.get_mandate(mid) or base.get_mandate(mid)
                print(f"  {mid}  {m.rail.value:9} {m.status.value:10} "
                      f"{fmt_inr(m.plan_amount_paise):>12}  {d['final_action']}{tag}")
                print(f"      grace audit {mid} --run {args.run}")

        show("CASE A - pre-emptive pause (no failure yet, salary nearby)",
             [(k, v) for k, v in ad.items()
              if v["trigger"] == "predicted" and v["final_action"] == "pause"])

        show("CASE B - cancel intent converted to a pause",
             [(k, v) for k, v in ad.items()
              if v["trigger"] == "intent" and v["final_action"] == "pause"])

        show("CASE B2 - cancel intent on eMandate (pause is the ONLY legal offer)",
             [(k, v) for k, v in ad.items()
              if v["trigger"] == "intent" and v["final_action"] == "pause"
              and (agent.get_mandate(k).rail.value == "emandate")])

        # The eMandate in-flight block first: "an automatic retry is already
        # scheduled" is also a guard block, but the double-debit window is the
        # one worth showing.
        show("CASE C - guard blocked a charge into the eMandate confirmation window (BASELINE arm)",
             [(k, v) for k, v in bd.items()
              if "in flight" in (v.get("gate_flags") or {}).get("integrity_blocked", "")])
        show("CASE C2 - guard blocked a charge with a retry already scheduled (BASELINE arm)",
             [(k, v) for k, v in bd.items()
              if "retry is scheduled" in (v.get("gate_flags") or {}).get("integrity_blocked", "")], n=2)
        print("      ^ use --arm baseline when auditing these")

        show("CASE D - escalated rather than guessed",
             [(k, v) for k, v in ad.items()
              if v["final_action"] == "escalate" and v["trigger"] == "failure"])

        show("CASE E - policy overrode the decision",
             [(k, v) for k, v in ad.items() if v.get("gate_flags")])

        # Show this one on purpose. The pitch promises the false-intervention
        # number rather than hiding it; here is what one actually looks like.
        show("CASE F - a FALSE intervention (healthy mandate acted on) - show this honestly",
             [(k, v) for k, v in ad.items()
              if v["final_action"] not in ("noop", "escalate")
              and (t := truth_of(k)) is not None and not t.will_fail],
             prefer_at_risk=False)
    finally:
        agent.close()
        base.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
