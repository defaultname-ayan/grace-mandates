"""The decision loop (spec 3).

Observe -> signal -> predict -> gate -> adjudicate -> policy -> integrity ->
act -> audit. Each arm runs against its own copy of the seeded database,
because actions mutate state and the arms must not contaminate each other.
"""
from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from typing import Any, Callable

from grace.adjudicate.schema import Decision
from grace.config import CONFIG
from grace.evidence import build_evidence, is_decision_trigger
from grace.integrity import IntegrityGuard
from grace.models import INTERVENTIONS, Action, Cause, Evidence, Mandate
from grace.policy.gate import gate
from grace.predict.features import featurise
from grace.predict.risk import LogisticRisk
from grace.rzp.sim import SimClient
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar
from grace.sim.engine import SimEngine
from grace.store import Store
from grace.util import stable_unit

SEED_DB = "grace.db"


def arm_db_path(run_dir: Path, arm: str) -> Path:
    return run_dir / f"arm_{arm}.db"


def prepare_arm_db(run_dir: Path, arm: str) -> Path:
    """Fresh copy of the seeded cohort for this arm."""
    src = run_dir / SEED_DB
    if not src.exists():
        raise FileNotFoundError(f"no seeded cohort at {src}; run `grace seed` first")
    dst = arm_db_path(run_dir, arm)
    for suffix in ("", "-wal", "-shm"):
        p = Path(str(dst) + suffix)
        if p.exists():
            p.unlink()
    shutil.copy2(src, dst)
    return dst


def _tally(values) -> dict[str, int]:
    out: dict[str, int] = {}
    for v in values:
        if v is not None:
            out[str(v)] = out.get(str(v), 0) + 1
    return dict(sorted(out.items(), key=lambda kv: -kv[1]))


def _noop_decision() -> Decision:
    return Decision(
        cause=Cause.UNKNOWN, cause_confidence=0.5, action=Action.NOOP, action_confidence=0.9,
        rationale="Below the intervention threshold: no failure, no customer signal.",
        evidence_used=[],
    )


def execute(client: SimClient, m: Mandate, action: Action, params: dict) -> Any:
    if action == Action.PAUSE:
        return client.pause(m.id, resume_on=params.get("resume_on"))
    if action == Action.RESUME:
        return client.resume(m.id, resume_on=params.get("resume_on"))
    if action == Action.CANCEL_AT_CYCLE_END:
        return client.cancel(m.id, at_cycle_end=True)
    if action == Action.MANUAL_CHARGE:
        return client.charge_invoice(m.id, params["invoice_id"])
    if action == Action.STEP_DOWN_PLAN:
        return client.update(m.id, plan_amount_paise=max(10000, int(m.plan_amount_paise * 0.6)))
    if action == Action.SHIFT_START:
        return client.update(m.id, start_at=params.get("start_at"))
    return None


def run_batch(
    run_dir: Path,
    arm: str,
    adjudicator=None,
    *,
    decision_date: date,
    guard_enabled: bool = True,
    max_workers: int = 1,
    limit: int | None = None,
    holdout_only: bool = False,
    sample: int | None = None,
    adjudicator_name: str = "none",
    on_progress: Callable[[int, int], None] | None = None,
) -> dict:
    """Run one arm end to end. Returns a summary dict."""
    db = prepare_arm_db(run_dir, arm)
    store = Store(db)
    try:
        bh, cal = BankHealth(), HolidayCalendar()
        engine = SimEngine(store, seed=CONFIG.seed, bank_health=bh, calendar=cal,
                           decision_date=decision_date)
        client = SimClient(store, engine)
        guard = IntegrityGuard(store, enabled=guard_enabled, now=engine.now)

        weights = run_dir / "risk_weights.json"
        model = LogisticRisk.load(weights) if weights.exists() else None

        mandates = store.all_mandates()
        if limit:
            mandates = mandates[:limit]
        run_id = f"{arm}-{uuid.uuid4().hex[:8]}"

        # ---- 1. evidence for every mandate
        bundles: list[tuple[Mandate, Evidence, str, bool]] = []
        for m in mandates:
            t = store.get_truth(m.id)
            intent = t.cancel_intent_text if t else None
            ev0 = build_evidence(store, m, bank_health=bh, calendar=cal, today=decision_date,
                                 cancel_intent_text=intent)
            p = model.predict(featurise(ev0)) if model else 0.0
            tau = model.preemptive_threshold if model else 0.60
            ev = ev0.model_copy(update={"p_fail": p, "preemptive_threshold": tau})
            triggered, trigger = is_decision_trigger(ev, CONFIG.theta_low)
            bundles.append((m, ev, trigger, triggered))

        # Restrict paid adjudication to the mandates the report actually scores.
        # Every metric is holdout-only, so adjudicating the training split costs
        # money and changes no reported number. Non-holdout mandates still get a
        # logged no-op decision, so n_scored stays comparable across arms.
        scored_ids = store.holdout_ids() if (holdout_only or sample) else None

        # A capped online sample. Free-tier quota makes a full online batch
        # impractical, so take a DETERMINISTIC subset of the triggered holdout
        # mandates and record exactly which ones, so every arm can be scored on
        # the same subset and the comparison stays paired.
        sampled_ids: set[str] | None = None
        if sample:
            eligible = sorted(
                m.id for m, ev, _, trig in bundles
                if trig and (scored_ids is None or m.id in scored_ids)
            )
            eligible.sort(key=lambda mid: stable_unit("online_sample", mid))
            sampled_ids = set(eligible[:sample])

        # ---- 2. adjudicate (parallel only where it is network-bound)
        decisions: dict[str, Decision] = {}
        fallbacks = 0
        to_decide = [(m, ev) for m, ev, _, trig in bundles
                     if trig and adjudicator is not None
                     and (scored_ids is None or m.id in scored_ids)
                     and (sampled_ids is None or m.id in sampled_ids)]

        def _one(pair):
            m, ev = pair
            from grace.adjudicate.claude import safe_default

            try:
                return m.id, adjudicator.decide(ev), None
            except Exception as e:  # never let one mandate kill the batch
                return m.id, safe_default(f"{type(e).__name__}: {e}"), str(e)

        if to_decide and max_workers > 1:
            with ThreadPoolExecutor(max_workers=max_workers) as pool:
                results = list(pool.map(_one, to_decide))
        else:
            results = [_one(p) for p in to_decide]

        for i, (mid, dec, err) in enumerate(results):
            decisions[mid] = dec
            if err:
                fallbacks += 1
            if on_progress:
                on_progress(i + 1, len(results))

        # ---- 3. gate, execute, audit
        summary = {
            "arm": arm, "run_id": run_id, "adjudicator": adjudicator_name,
            "holdout_only_adjudication": holdout_only,
            "sample_size": sample,
            "sampled_ids": sorted(sampled_ids) if sampled_ids else None,
            "n_mandates": len(mandates), "n_triggered": sum(1 for *_, t in bundles if t),
            "adjudicator_fallbacks": fallbacks, "guard_enabled": guard_enabled,
            "actions": {}, "flags": {}, "triggers": {}, "executed": 0, "execution_errors": 0,
            "double_debits_detected": 0, "double_debits_prevented": 0,
        }

        for m, ev, trigger, triggered in bundles:
            decision = decisions.get(m.id) or _noop_decision()
            summary["triggers"][trigger] = summary["triggers"].get(trigger, 0) + 1

            res = gate(decision, ev, guard=guard, store=store, today=decision_date, calendar=cal)
            final, params, flags = res.final_action, res.params, res.flags

            decision_id = f"{arm}:{m.id}"
            store.append_audit(
                phase="intent", run_id=run_id, decision_id=decision_id, mandate_id=m.id,
                rail=m.rail.value, status_before=m.status.value, trigger=trigger,
                p_fail=round(ev.p_fail, 4), cause=decision.cause.value,
                cause_conf=decision.cause_confidence, proposed_action=decision.action.value,
                final_action=final.value, action_conf=decision.action_confidence,
                params=params, gate_flags=flags, rationale=decision.rationale,
                evidence_used=decision.evidence_used, customer_message=decision.customer_message,
                escalate=decision.escalate, adjudicator=adjudicator_name,
                rupees_at_stake=m.plan_amount_paise * min(3, m.remaining_count),
            )

            executed, api_req, api_res, err = False, {}, {}, None
            if final in INTERVENTIONS:
                result = execute(client, m, final, params)
                if result is not None:
                    executed = result.ok
                    api_req, api_res, err = result.request, result.response, result.error
                    if not result.ok:
                        summary["execution_errors"] += 1
                    if executed:
                        summary["executed"] += 1
                        m = store.get_mandate(m.id) or m
                        m.interventions_this_cycle += 1
                        m.interventions_total += 1
                        store.upsert_mandate(m)

            after = store.get_mandate(m.id) or m
            store.append_audit(
                phase="result", run_id=run_id, decision_id=decision_id, mandate_id=m.id,
                final_action=final.value, executed=executed, api_request=api_req,
                api_response={k: api_res.get(k) for k in ("id", "status", "charge_at", "paused_at")}
                if api_res else {},
                error=err, status_after=after.status.value,
            )

            store.save_decision(decision_id, m.id, arm, {
                "trigger": trigger, "p_fail": ev.p_fail,
                "cause": decision.cause.value, "cause_conf": decision.cause_confidence,
                "proposed_action": decision.action.value, "final_action": final.value,
                "action_conf": decision.action_confidence, "params": params,
                "gate_flags": flags, "rationale": decision.rationale,
                "evidence_used": decision.evidence_used, "escalate": decision.escalate,
                "executed": executed, "error": err,
                "rupees_at_stake": m.plan_amount_paise * min(3, m.remaining_count),
                "adjudicator": adjudicator_name,
                "days_to_salary": ev.days_to_salary,
                "emandate_attempt_in_flight": ev.emandate_attempt_in_flight,
            })

            summary["actions"][final.value] = summary["actions"].get(final.value, 0) + 1
            for k in flags:
                summary["flags"][k] = summary["flags"].get(k, 0) + 1

        # ---- 4. integrity accounting
        for blocked in guard.blocked:
            st = store.get_sim_state(blocked["mandate_id"])
            would_have_landed = any(
                f["invoice_id"] == blocked["invoice_id"] and f.get("will_succeed")
                for f in st.get("inflight", [])
            )
            if would_have_landed or blocked["attempt_in_flight"]:
                summary["double_debits_prevented"] += 1
        summary["guard_blocks"] = len(guard.blocked)
        summary["double_debits_detected"] = sum(
            guard.scan_for_double_debits(m.id) for m in mandates
        )
        metas = list(getattr(adjudicator, "metas", []) or [])
        if metas:
            summary["llm"] = {
                "calls": len(metas),
                "input_tokens": sum(x.get("input_tokens", 0) for x in metas),
                "output_tokens": sum(x.get("output_tokens", 0) for x in metas),
                "cache_read_input_tokens": sum(x.get("cache_read_input_tokens", 0) for x in metas),
                "mean_latency_ms": round(sum(x.get("latency_ms", 0) for x in metas) / len(metas)),
                "thinking_tokens": sum(x.get("thinking_tokens", 0) for x in metas),
                "requested_model": metas[0].get("requested_model") or metas[0].get("model"),
                "effort": metas[0].get("effort"),
                # Which model actually served each call. A run mostly served by a
                # fallback model is a different experiment and must not be
                # reported as a result for the requested model.
                "served_by": _tally(x.get("model") for x in metas),
                "fallbacks_used": sum(1 for x in metas if x.get("fallback_depth", 0) > 0),
            }
        store.set_meta(f"summary_{arm}", summary)
        return summary
    finally:
        store.close()
