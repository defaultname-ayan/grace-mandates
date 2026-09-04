"""Evaluation driver (spec 14.3): run every arm, score the holdout, write eval.json."""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from grace.adjudicate.offline import OfflineAdjudicator
from grace.config import CONFIG
from grace.evaluation.baseline import RulesBaseline
from grace.evaluation.metrics import compare, evaluate_arm
from grace.orchestrator import arm_db_path, run_batch
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar
from grace.store import Store

ARMS = ("noop", "baseline", "agent")


def make_adjudicator(arm: str, *, offline: bool, today: date, effort: str | None = None):
    if arm == "noop":
        return None, "none"
    if arm == "baseline":
        return RulesBaseline(today=today, calendar=HolidayCalendar()), "rules_baseline"
    if offline:
        return OfflineAdjudicator(today=today, calendar=HolidayCalendar()), "offline_stub"
    from grace.adjudicate import make_llm_adjudicator

    adj = make_llm_adjudicator(effort=effort)
    return adj, f"{adj.name}:{adj.model}:{adj.effort}"


def run_all(
    run_dir: Path,
    *,
    decision_date: date,
    offline: bool = True,
    guard_enabled: bool = True,
    effort: str | None = None,
    limit: int | None = None,
    holdout_only: bool = False,
    sample: int | None = None,
    arms: tuple[str, ...] = ARMS,
    max_workers: int | None = None,
    on_progress=None,
) -> dict:
    summaries: dict[str, dict] = {}
    for arm in arms:
        adj, name = make_adjudicator(arm, offline=offline, today=decision_date, effort=effort)
        workers = 1 if (arm != "agent" or offline) else (max_workers or CONFIG.max_workers)
        summaries[arm] = run_batch(
            run_dir, arm, adj, decision_date=decision_date, guard_enabled=guard_enabled,
            max_workers=workers, limit=limit, holdout_only=holdout_only, sample=sample,
            adjudicator_name=name,
            on_progress=(lambda i, t, a=arm: on_progress(a, i, t)) if on_progress else None,
        )
    return summaries


def score(run_dir: Path, arms: tuple[str, ...] = ARMS,
          restrict_to: set[str] | None = None, *, on_sample: bool = False) -> dict:
    """Score every arm on the holdout and write eval.json.

    `on_sample` restricts scoring to the deterministic subset recorded by the
    most recent `--sample` run, for every arm, so a quota-limited online run is
    still a paired comparison. The restriction is recorded in the payload so
    eval.json can never be mistaken for a full-holdout result.
    """
    stores: dict[str, Store] = {}
    summaries: dict[str, dict] = {}
    results: dict[str, dict] = {}
    predictor: dict = {}
    manifest: dict = {}
    sample_from: str | None = None
    try:
        for arm in arms:
            db = arm_db_path(run_dir, arm)
            if db.exists():
                stores[arm] = Store(db)
                summaries[arm] = stores[arm].get_meta(f"summary_{arm}", {}) or {}
        if on_sample and restrict_to is None:
            for arm, summ in summaries.items():
                if summ.get("sampled_ids"):
                    restrict_to, sample_from = set(summ["sampled_ids"]), arm
                    break
        for arm, s in stores.items():
            results[arm] = evaluate_arm(s, arm, holdout_only=True, only_ids=restrict_to)
            results[arm]["batch"] = summaries[arm]
            if not predictor:
                predictor = s.get_meta("predictor", {})
                manifest = s.get_meta("manifest", {})
    finally:
        for s in stores.values():
            s.close()

    # A partial arm (e.g. a previous --limit run) would otherwise be scored
    # against full arms and silently produce a nonsense comparison.
    scored = {a: r["n_scored"] for a, r in results.items()}
    mismatch = len(set(scored.values())) > 1
    if mismatch:
        widest = max(scored.values())
        for arm, n in scored.items():
            if n < widest:
                results[arm]["PARTIAL_RUN_WARNING"] = (
                    f"This arm scored only {n} of {widest} holdout mandates. It was probably run "
                    f"with --limit, or interrupted. Re-run `grace run-batch` for all arms before "
                    f"comparing."
                )

    bh = BankHealth()
    payload = {
        "HONESTY": {
            "cohort": "SYNTHETIC. Every mandate, amount and outcome is generated. Rupee figures "
                      "are illustrative and must not be presented as observed merchant results.",
            "bank_health_provenance": bh.provenance,
            "bank_health_is_synthetic": bh.is_synthetic,
            "outcome_model": "Outcomes are drawn from the cohort's counterfactual survival table "
                             "using common random numbers shared across arms, not from live "
                             "payment behaviour.",
            "holdout_only": True,
        },
        "sample": (
            {"size": len(restrict_to), "recorded_by_arm": sample_from,
             "note": "All arms scored on this subset only. NOT a full-holdout result."}
            if restrict_to is not None else None
        ),
        "arms": results,
        "arms_comparable": not mismatch,
        "n_scored_per_arm": scored,
        "comparison": compare(results),
        "predictor": predictor,
        "cohort_manifest": manifest,
    }
    if any(r["batch"].get("adjudicator") == "offline_stub" for r in results.values()):
        payload["HONESTY"]["agent_column"] = (
            "The agent arm ran the OFFLINE STUB, not a model. The stub is deterministic and its "
            "intent lexicon was tuned on the same templates that generate this cohort, so its "
            "intent accuracy here is circular. Treat the agent column as a pipeline check, not a "
            "model result. Re-run with --online for a real measurement."
        )
    (run_dir / "eval.json").write_text(json.dumps(payload, indent=2, default=str))
    return payload
