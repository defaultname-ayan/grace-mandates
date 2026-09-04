"""Self-contained HTML report (spec 14.4). No CDN, no JS frameworks."""
from __future__ import annotations

import json
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from grace.models import ACTION_TO_CF_KEY, CF_KEYS, Action
from grace.orchestrator import arm_db_path
from grace.store import Store
from grace.util import fmt_inr

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def _env() -> Environment:
    env = Environment(loader=FileSystemLoader(TEMPLATES),
                      autoescape=select_autoescape(["html"]))
    env.filters["inr"] = fmt_inr
    env.filters["pct"] = lambda v: "-" if v is None else f"{v:.1%}"
    env.filters["num"] = lambda v: "-" if v is None else f"{v:,}"
    env.filters["f4"] = lambda v: "-" if v is None else f"{v:.4f}"
    return env


def worst_decisions(run_dir: Path, arm: str, k: int = 10) -> list[dict]:
    """The k highest-regret decisions, with their rationales. What we got wrong."""
    db = arm_db_path(run_dir, arm)
    if not db.exists():
        return []
    s = Store(db)
    try:
        holdout = s.holdout_ids()
        rows = []
        for mid, d in s.decisions_for_arm(arm).items():
            if mid not in holdout or d.get("trigger") == "tick":
                continue
            t = s.get_truth(mid)
            m = s.get_mandate(mid)
            if not t or not m or not t.survival_under:
                continue
            best = max(t.survival_under.get(x, 0.0) for x in CF_KEYS)
            got = t.survival_under.get(ACTION_TO_CF_KEY.get(Action(d["final_action"]), "noop"), 0.0)
            rows.append({
                "mandate_id": mid, "rail": m.rail.value, "status": m.status.value,
                "regret": round(best - got, 3), "true_cause": t.cause.value,
                "guessed_cause": d.get("cause"), "final_action": d["final_action"],
                "proposed_action": d.get("proposed_action"),
                "rationale": d.get("rationale", ""), "flags": d.get("gate_flags", {}),
                "amount": m.plan_amount_paise,
            })
        rows.sort(key=lambda r: -r["regret"])
        return rows[:k]
    finally:
        s.close()


def top_by_stake(run_dir: Path, arm: str, k: int = 10) -> list[dict]:
    db = arm_db_path(run_dir, arm)
    if not db.exists():
        return []
    s = Store(db)
    try:
        rows = []
        for mid, d in s.decisions_for_arm(arm).items():
            if d.get("final_action") == "noop":
                continue
            m = s.get_mandate(mid)
            rows.append({
                "mandate_id": mid, "rail": m.rail.value if m else "?",
                "final_action": d["final_action"], "cause": d.get("cause"),
                "stake": d.get("rupees_at_stake", 0), "rationale": d.get("rationale", ""),
                "flags": d.get("gate_flags", {}),
            })
        rows.sort(key=lambda r: -r["stake"])
        return rows[:k]
    finally:
        s.close()


def render(run_dir: Path, out_name: str = "report.html") -> Path:
    eval_path = run_dir / "eval.json"
    if not eval_path.exists():
        raise FileNotFoundError(f"{eval_path} missing; run `grace eval` first")
    payload = json.loads(eval_path.read_text())

    arms = payload["arms"]
    agent_arm = "agent" if "agent" in arms else next(iter(arms), None)
    ctx = {
        "payload": payload,
        "arms": arms,
        "arm_names": list(arms),
        "comparison": payload.get("comparison", {}),
        "predictor": payload.get("predictor", {}),
        "manifest": payload.get("cohort_manifest", {}),
        "honesty": payload.get("HONESTY", {}),
        "worst": worst_decisions(run_dir, agent_arm) if agent_arm else [],
        "top_stake": top_by_stake(run_dir, agent_arm) if agent_arm else [],
        "metric_rows": [
            ("Mandates scored (holdout)", "n_scored", "num"),
            ("At risk", "at_risk", "num"),
            ("Mandates preserved", "mandates_preserved", "num"),
            ("Rupees preserved", "rupees_preserved_paise", "inr"),
            ("Preservation rate", "preservation_rate", "pct"),
            ("Interventions", "interventions", "num"),
            ("False interventions", "false_interventions", "num"),
            ("False-intervention rate", "false_intervention_rate", "pct"),
            ("False-intervention cost", "false_intervention_cost_paise", "inr"),
            ("Escalation rate", "escalation_rate", "pct"),
            ("Cause accuracy", "cause_accuracy", "pct"),
            ("Action regret (lower is better)", "action_regret", "f4"),
            ("Intent conversion rate", "intent_conversion_rate", "pct"),
            ("Out-of-policy overrides", "model_out_of_policy", "num"),
            ("Adjudicator fallbacks", "adjudicator_fallbacks", "num"),
        ],
    }
    html = _env().get_template("report.html").render(**ctx)
    out = run_dir / out_name
    out.write_text(html)
    return out
