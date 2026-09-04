"""Cancel-intent converter (spec 12).

The moment a customer says "cancel" is the moment the middle state is worth
most. This turns that message into a bounded offer rather than a termination.
On UPI and eMandate the only offer Razorpay permits is a pause, which is
exactly the product's point.
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

from grace.config import CONFIG
from grace.evidence import build_evidence
from grace.integrity import IntegrityGuard
from grace.models import Action
from grace.orchestrator import arm_db_path, execute
from grace.policy.gate import gate
from grace.predict.features import featurise
from grace.predict.risk import LogisticRisk
from grace.rzp.sim import SimClient
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar
from grace.sim.engine import SimEngine
from grace.store import Store

OFFER_LABEL = {
    Action.PAUSE: "pause",
    Action.STEP_DOWN_PLAN: "step_down_plan",
    Action.CANCEL_AT_CYCLE_END: "cancel_at_cycle_end",
}


def convert(
    run_dir: Path,
    arm: str,
    mandate_id: str,
    text: str,
    *,
    offline: bool = True,
    today: date | None = None,
    execute_now: bool = False,
) -> dict:
    today = today or date.today()
    store = Store(arm_db_path(run_dir, arm))
    try:
        m = store.get_mandate(mandate_id)
        if m is None:
            raise KeyError(f"unknown mandate {mandate_id}")
        bh, cal = BankHealth(), HolidayCalendar()
        ev0 = build_evidence(store, m, bank_health=bh, calendar=cal, today=today,
                             cancel_intent_text=text)
        weights = run_dir / "risk_weights.json"
        tau = 0.60
        p = 0.0
        if weights.exists():
            _model = LogisticRisk.load(weights)
            p, tau = _model.predict(featurise(ev0)), _model.preemptive_threshold
        ev = ev0.model_copy(update={"p_fail": p, "preemptive_threshold": tau})

        if offline:
            from grace.adjudicate.offline import OfflineAdjudicator

            adj = OfflineAdjudicator(today=today, calendar=cal)
            name = "offline_stub"
        else:
            from grace.adjudicate import make_llm_adjudicator

            adj = make_llm_adjudicator()
            name = f"{adj.name}:{adj.model}"

        decision = adj.decide(ev)
        engine = SimEngine(store, seed=CONFIG.seed, bank_health=bh, calendar=cal,
                           decision_date=today)
        guard = IntegrityGuard(store, now=engine.now)
        res = gate(decision, ev, guard=guard, store=store, today=today, calendar=cal)

        offer = None
        if res.final_action in OFFER_LABEL:
            offer = {
                "type": OFFER_LABEL[res.final_action],
                "cycles": res.params.get("cycles"),
                "resume_on": res.params.get("resume_on"),
                "rail": m.rail.value,
                "reversible": res.final_action != Action.CANCEL_AT_CYCLE_END,
            }

        executed = False
        if execute_now and res.final_action in OFFER_LABEL:
            client = SimClient(store, engine)
            out = execute(client, m, res.final_action, res.params)
            executed = bool(out and out.ok)

        store.append_audit(
            phase="intent_conversion", mandate_id=mandate_id,
            decision_id=f"intent:{mandate_id}", trigger="intent", intent_text=text,
            cause=decision.cause.value, cause_conf=decision.cause_confidence,
            proposed_action=decision.action.value, final_action=res.final_action.value,
            gate_flags=res.flags, offer=offer, executed=executed, adjudicator=name,
            rationale=decision.rationale,
        )
        return {
            "mandate_id": mandate_id, "rail": m.rail.value, "status": m.status.value,
            "intent_text": text, "cause": decision.cause.value,
            "cause_confidence": decision.cause_confidence,
            "proposed_action": decision.action.value,
            "final_action": res.final_action.value, "gate_flags": res.flags,
            "offer": offer, "rationale": decision.rationale,
            "customer_message": decision.customer_message, "executed": executed,
            "adjudicator": name,
            "allowed_actions": [a.value for a in ev.allowed_actions],
        }
    finally:
        store.close()
