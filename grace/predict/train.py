"""Train the risk model on the training split only (spec 7)."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from grace.evidence import build_evidence
from grace.predict.features import FEATURE_NAMES, featurise
from grace.predict.risk import LogisticRisk, brier, calibration_table
from grace.signals.bank_health import BankHealth
from grace.signals.holidays import HolidayCalendar
from grace.store import Store


def build_dataset(store: Store, today: date, *, holdout: bool | None = None):
    bh, cal = BankHealth(), HolidayCalendar()
    X, y, ids = [], [], []
    for m in store.all_mandates(holdout=holdout):
        t = store.get_truth(m.id)
        if t is None:
            continue
        ev = build_evidence(store, m, bank_health=bh, calendar=cal, today=today,
                            cancel_intent_text=t.cancel_intent_text)
        X.append(featurise(ev))
        y.append(int(t.will_fail))
        ids.append(m.id)
    return X, y, ids


def tune_threshold(store: Store, model, today: date) -> tuple[float, list[dict]]:
    """Pick the pre-emptive-pause threshold on the TRAINING split.

    For each candidate threshold, sum the counterfactual value of pausing every
    training mandate above it: at-risk mandates gain (pause - noop), healthy
    mandates lose it. The argmax is the risk level at which acting pays for
    itself. The holdout is never consulted.
    """
    from grace.signals.bank_health import BankHealth
    from grace.signals.holidays import HolidayCalendar

    bh, cal = BankHealth(), HolidayCalendar()
    rows = []
    for m in store.all_mandates(holdout=False):
        t = store.get_truth(m.id)
        if t is None or not t.survival_under:
            continue
        ev = build_evidence(store, m, bank_health=bh, calendar=cal, today=today,
                            cancel_intent_text=t.cancel_intent_text)
        if ev.cancel_intent_text:
            continue  # intents are handled by their own trigger, not by risk
        gain = (t.survival_under.get("pause", 0.0) - t.survival_under.get("noop", 0.0))
        rows.append((model.predict(featurise(ev)), gain * m.rupees_at_stake))

    sweep = []
    for i in range(1, 20):
        tau = i / 20.0
        net = sum(v for p, v in rows if p >= tau)
        acted = sum(1 for p, _ in rows if p >= tau)
        sweep.append({"threshold": round(tau, 2), "acted": acted, "net_value_paise": int(net)})
    best = max(sweep, key=lambda r: r["net_value_paise"])
    return best["threshold"], sweep


def train(store: Store, run_dir: Path, today: date) -> dict:
    """Fit on non-holdout only, then report honestly on the holdout."""
    Xtr, ytr, _ = build_dataset(store, today, holdout=False)
    model = LogisticRisk(FEATURE_NAMES).fit(Xtr, ytr)
    tau, sweep = tune_threshold(store, model, today)
    model.preemptive_threshold = tau
    model.save(run_dir / "risk_weights.json")

    Xho, yho, _ = build_dataset(store, today, holdout=True)
    p_ho = [model.predict(x) for x in Xho]
    p_tr = [model.predict(x) for x in Xtr]

    base = sum(ytr) / len(ytr) if ytr else 0.0
    stats = {
        "train": {"n": len(ytr), "positives": sum(ytr), "brier": round(brier(p_tr, ytr), 4)},
        "holdout": {
            "n": len(yho), "positives": sum(yho), "brier": round(brier(p_ho, yho), 4),
            "calibration": calibration_table(p_ho, yho),
        },
        "baseline_brier_predicting_base_rate": round(brier([base] * len(yho), yho), 4),
        "top_weights": [{"feature": n, "weight": round(w, 3)} for n, w in model.top_weights(10)],
        "preemptive_threshold": tau,
        "threshold_sweep": sweep,
        "base_rate_brier": None,
        "note": "Trained on the 70% training split only. The holdout is never used for fitting "
                "or threshold selection. The pre-emptive-pause threshold is the argmax of "
                "counterfactual net value on the training split.",
    }
    store.set_meta("predictor", stats)
    return stats
