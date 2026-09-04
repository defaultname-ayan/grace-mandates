"""Holdout metrics (spec 14.1).

Outcomes are scored against the cohort's counterfactual survival table using
COMMON RANDOM NUMBERS: every mandate gets one fixed uniform draw, shared across
all arms. Arms therefore differ only by the action they chose, never by luck.
"""
from __future__ import annotations

from collections import Counter

from grace.models import ACTION_TO_CF_KEY, CF_KEYS, INTERVENTIONS, Action
from grace.store import Store
from grace.util import stable_unit

#: Actions that keep a mandate in a billing relationship rather than ending it.
MIDDLE_STATES = {Action.PAUSE, Action.STEP_DOWN_PLAN, Action.RESUME, Action.SHIFT_START}


def outcome_draw(mandate_id: str) -> float:
    """One fixed uniform per mandate, shared by every arm."""
    return stable_unit("outcome", mandate_id)


def survived(truth, action: Action, mandate_id: str) -> bool:
    key = ACTION_TO_CF_KEY.get(action, "noop")
    p = truth.survival_under.get(key, truth.survival_under.get("noop", 0.5))
    return outcome_draw(mandate_id) < p


def evaluate_arm(store: Store, arm: str, *, holdout_only: bool = True,
                 only_ids: set[str] | None = None) -> dict:
    decisions = store.decisions_for_arm(arm)
    ids = store.holdout_ids() if holdout_only else {m.id for m in store.all_mandates()}
    if only_ids is not None:
        ids = ids & only_ids

    n = adjudicated = escalations = interventions = false_interventions = 0
    preserved = preserved_rupees = at_risk = at_risk_rupees = 0
    cause_hits = cause_total = 0
    regret_sum = regret_n = 0.0
    fi_cost = 0
    out_of_policy = fallbacks = 0
    intent_total = intent_converted = 0
    flag_counts: Counter[str] = Counter()
    action_counts: Counter[str] = Counter()
    survived_rupees = 0

    for mid, d in decisions.items():
        if mid not in ids:
            continue
        m = store.get_mandate(mid)
        t = store.get_truth(mid)
        if m is None or t is None:
            continue
        n += 1
        final = Action(d["final_action"])
        action_counts[final.value] += 1
        rup = d.get("rupees_at_stake") or m.rupees_at_stake

        # keys only: Counter.update(dict) would treat the reason STRINGS as counts
        flag_counts.update((d.get("gate_flags") or {}).keys())
        if "model_out_of_policy" in d.get("gate_flags", {}):
            out_of_policy += 1
        if d.get("trigger") != "tick":
            adjudicated += 1
            if d.get("cause") and t.will_fail:
                cause_total += 1
                cause_hits += int(d["cause"] == t.cause.value)
        if final == Action.ESCALATE:
            escalations += 1
        if d.get("fallback"):
            fallbacks += 1

        alive = survived(t, final, mid)
        if alive:
            survived_rupees += rup
        if t.will_fail:
            at_risk += 1
            at_risk_rupees += rup
            if alive:
                preserved += 1
                preserved_rupees += rup

        if final in INTERVENTIONS:
            interventions += 1
            if not t.will_fail:
                false_interventions += 1
                delta = t.survival_under.get("noop", 0.97) - t.survival_under.get(
                    ACTION_TO_CF_KEY.get(final, "noop"), 0.97)
                fi_cost += int(max(0.0, delta) * rup)

        if d.get("trigger") != "tick" and t.survival_under:
            best = max(t.survival_under.get(k, 0.0) for k in CF_KEYS)
            got = t.survival_under.get(ACTION_TO_CF_KEY.get(final, "noop"), 0.0)
            regret_sum += max(0.0, best - got)
            regret_n += 1

        if t.cancel_intent != "none":
            intent_total += 1
            if final in MIDDLE_STATES:
                intent_converted += 1

    def pct(a: int, b: int) -> float | None:
        return round(a / b, 4) if b else None

    return {
        "arm": arm,
        "n_scored": n,
        "adjudicated": adjudicated,
        "at_risk": at_risk,
        "at_risk_rupees_paise": at_risk_rupees,
        "mandates_preserved": preserved,
        "rupees_preserved_paise": preserved_rupees,
        "survived_rupees_paise": survived_rupees,
        "preservation_rate": pct(preserved, at_risk),
        "interventions": interventions,
        "false_interventions": false_interventions,
        "false_intervention_rate": pct(false_interventions, interventions),
        "false_intervention_cost_paise": fi_cost,
        "escalations": escalations,
        "escalation_rate": pct(escalations, adjudicated),
        "cause_accuracy": pct(cause_hits, cause_total),
        "cause_scored": cause_total,
        "action_regret": round(regret_sum / regret_n, 4) if regret_n else None,
        "model_out_of_policy": out_of_policy,
        "model_out_of_policy_rate": pct(out_of_policy, adjudicated),
        "adjudicator_fallbacks": fallbacks,
        "adjudicator_fallback_rate": pct(fallbacks, adjudicated),
        "intent_total": intent_total,
        "intent_converted": intent_converted,
        "intent_conversion_rate": pct(intent_converted, intent_total),
        "gate_flags": dict(flag_counts),
        "actions": dict(action_counts),
    }


def compare(arms: dict[str, dict]) -> dict:
    """Lift of each arm over the noop reference."""
    ref = arms.get("noop")
    if not ref:
        return {}
    out = {}
    for name, a in arms.items():
        if name == "noop":
            continue
        out[name] = {
            "rupees_preserved_lift_paise": a["rupees_preserved_paise"] - ref["rupees_preserved_paise"],
            "mandates_preserved_lift": a["mandates_preserved"] - ref["mandates_preserved"],
            "false_intervention_rate": a["false_intervention_rate"],
            "false_intervention_cost_paise": a["false_intervention_cost_paise"],
            "net_rupees_paise": (a["rupees_preserved_paise"] - ref["rupees_preserved_paise"])
                                - a["false_intervention_cost_paise"],
        }
    return out
