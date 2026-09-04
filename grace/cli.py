"""Grace CLI (spec 15)."""
from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any, Callable

import typer

from grace.config import CONFIG
from grace.util import fmt_inr

app = typer.Typer(add_completion=False, help="Grace - the missing middle state for Indian auto-debit.")
RUNS = Path("runs")
DEFAULT_DECISION_DATE = date(2026, 9, 4)


def _run_dir(run: str) -> Path:
    return RUNS / run


def _echo_kv(label: str, value, width: int = 34) -> None:
    typer.echo(f"  {label:<{width}} {value}")


@app.command()
def seed(
    n: int = typer.Option(2000, help="Number of mandates."),
    seed_value: int = typer.Option(CONFIG.seed, "--seed", help="RNG seed."),
    run: str = typer.Option("demo", help="Run name under runs/."),
    months: int = typer.Option(6, help="Months of history per mandate."),
) -> None:
    """Generate the synthetic cohort, its history and its ground truth."""
    from grace.predict.train import train
    from grace.sim.cohort import generate, write_cohort_files
    from grace.store import Store

    rd = _run_dir(run)
    rd.mkdir(parents=True, exist_ok=True)
    for p in rd.glob("*.db*"):
        p.unlink()

    store = Store(rd / "grace.db")
    try:
        typer.secho(f"Generating {n} synthetic mandates (seed={seed_value})...", fg="cyan")
        mf = generate(store, n=n, seed=seed_value, decision_date=DEFAULT_DECISION_DATE,
                      history_months=months)
        write_cohort_files(store, rd)
        typer.secho("Training risk model on the 70% training split...", fg="cyan")
        stats = train(store, rd, DEFAULT_DECISION_DATE)
    finally:
        store.close()

    c = mf["realised_counts"]
    typer.secho("\nCohort", fg="green", bold=True)
    _echo_kv("mandates", n)
    _echo_kv("rails", c["rail"])
    _echo_kv("status at decision point", c["status"])
    _echo_kv("at-risk breakdown", c["at_risk_reason"])
    _echo_kv("holdout", f"{c['holdout']} ({c['holdout']/n:.1%})")
    typer.secho("\nPredictor (holdout)", fg="green", bold=True)
    _echo_kv("brier", stats["holdout"]["brier"])
    _echo_kv("positives", f"{stats['holdout']['positives']}/{stats['holdout']['n']}")
    for w in stats["top_weights"][:5]:
        _echo_kv(f"  weight {w['feature']}", w["weight"])
    typer.secho(f"\nWrote {rd}/cohort.csv, cohort_manifest.json, risk_weights.json", fg="green")
    typer.secho("All data is SYNTHETIC.", fg="yellow")


@app.command("run-batch")
def run_batch_cmd(
    run: str = typer.Option("demo"),
    offline: bool = typer.Option(True, "--offline/--online",
                                 help="--offline uses the deterministic stub; --online calls Claude."),
    guard: bool = typer.Option(True, "--guard/--no-guard",
                               help="--no-guard disables the double-debit guard (counterfactual)."),
    effort: str = typer.Option(None, help="Claude effort for --online (default GRACE_BATCH_EFFORT)."),
    limit: int = typer.Option(None, help="Only process the first N mandates."),
    arms: str = typer.Option("noop,baseline,agent", help="Comma-separated arms to run."),
    holdout_only: bool = typer.Option(False, "--holdout-only",
                                      help="Only adjudicate holdout mandates. Every reported "
                                           "metric is holdout-only, so this changes no number "
                                           "and cuts paid calls by ~70%."),
    workers: int = typer.Option(None, help="Concurrent adjudications for --online."),
    sample: int = typer.Option(None, help="Adjudicate only N deterministically-chosen triggered "
                                          "holdout mandates. For --online runs where provider "
                                          "quota makes a full batch impractical."),
) -> None:
    """Run the decision loop for each arm against its own copy of the cohort."""
    from grace.evaluation.run import run_all

    rd = _run_dir(run)
    if not (rd / "grace.db").exists():
        typer.secho(f"No cohort at {rd}. Run `grace seed --run {run}` first.", fg="red")
        raise typer.Exit(1)

    arm_tuple = tuple(a.strip() for a in arms.split(",") if a.strip())
    if not offline:
        from grace.adjudicate import credentials_present, sdk_present

        ok, why = sdk_present()
        if not ok:
            typer.secho(why, fg="red")
            raise typer.Exit(2)
        ok, why = credentials_present()
        if not ok:
            typer.secho(why, fg="red")
            typer.secho(
                "Checked before the run starts: with GRACE_PROVIDER=anthropic the client does "
                "not raise without a key, so every adjudication would fall back to escalate and "
                "the run would still exit 0 and look successful.",
                fg="yellow")
            raise typer.Exit(2)
        effort = effort or CONFIG.batch_effort
        typer.secho(f"ONLINE: provider={CONFIG.provider} effort={effort}. This costs money.",
                    fg="yellow")

    def progress(arm, i, total):
        if total and (i % 25 == 0 or i == total):
            typer.echo(f"  [{arm}] adjudicated {i}/{total}")

    summaries = run_all(rd, decision_date=DEFAULT_DECISION_DATE, offline=offline,
                        guard_enabled=guard, effort=effort, limit=limit,
                        holdout_only=holdout_only, sample=sample, arms=arm_tuple,
                        max_workers=workers, on_progress=progress)

    for arm, s in summaries.items():
        typer.secho(f"\n{arm}  ({s['adjudicator']})", fg="green", bold=True)
        _echo_kv("mandates", s["n_mandates"])
        _echo_kv("triggered for adjudication", s["n_triggered"])
        _echo_kv("actions", s["actions"])
        _echo_kv("gate overrides", s["flags"] or "none")
        _echo_kv("executed", s["executed"])
        _echo_kv("guard blocks", s.get("guard_blocks", 0))
        _echo_kv("double debits prevented", s["double_debits_prevented"])
        _echo_kv("double debits DETECTED", s["double_debits_detected"])
        if s.get("llm"):
            _echo_kv("llm", s["llm"])
    if not guard:
        typer.secho("\n--no-guard: the double-debit guard was OFF for this run.", fg="red", bold=True)


@app.command("eval")
def eval_cmd(
    run: str = typer.Option("demo"),
    arms: str = typer.Option("noop,baseline,agent"),
    on_model_decided: bool = typer.Option(
        False, "--on-model-decided",
        help="Score every arm on the mandates the model actually decided, excluding those the "
             "safety fallback escalated after a provider failure. For quota-truncated online runs."),
    on_sample: bool = typer.Option(False, "--on-sample",
                                   help="Score every arm on the sampled subset recorded by the "
                                        "last --sample run, so the comparison stays paired."),
) -> None:
    """Score the holdout and write eval.json."""

    from grace.evaluation.run import score

    rd = _run_dir(run)
    arm_tuple = tuple(a.strip() for a in arms.split(",") if a.strip())
    payload = score(rd, arm_tuple, on_sample=on_sample, on_model_decided=on_model_decided)
    if payload.get("sample"):
        smp = payload["sample"]
        typer.secho(f"Scoring all arms on the {smp['size']}-mandate sample recorded by arm "
                    f"'{smp['recorded_by_arm']}'. NOT a full-holdout result.", fg="cyan")
    elif on_sample or on_model_decided:
        typer.secho("No restricted run found; scoring the full holdout.", fg="yellow")
    res = payload["arms"]
    if not res:
        typer.secho("No arm results. Run `grace run-batch` first.", fg="red")
        raise typer.Exit(1)

    if not payload.get("arms_comparable", True):
        typer.secho(
            "\nWARNING: arms scored different numbers of mandates "
            f"({payload['n_scored_per_arm']}). One arm was probably run with --limit or "
            "interrupted; the comparison below is NOT valid. Re-run `grace run-batch` for all arms.",
            fg="red", bold=True)

    typer.secho("\nHoldout results (synthetic cohort)", fg="green", bold=True)
    cols = list(res)
    rows: list[tuple[str, str, Callable[[Any], str]]] = [
        ("mandates scored", "n_scored", str),
        ("at risk", "at_risk", str),
        ("mandates preserved", "mandates_preserved", str),
        ("rupees preserved", "rupees_preserved_paise", fmt_inr),
        ("preservation rate", "preservation_rate", lambda v: f"{v:.1%}" if v is not None else "-"),
        ("interventions", "interventions", str),
        ("false interventions", "false_interventions", str),
        ("false-intervention rate", "false_intervention_rate", lambda v: f"{v:.1%}" if v is not None else "-"),
        ("false-intervention cost", "false_intervention_cost_paise", fmt_inr),
        ("escalation rate", "escalation_rate", lambda v: f"{v:.1%}" if v is not None else "-"),
        ("cause accuracy", "cause_accuracy", lambda v: f"{v:.1%}" if v is not None else "-"),
        ("action regret (lower better)", "action_regret", lambda v: f"{v:.4f}" if v is not None else "-"),
        ("intent conversion", "intent_conversion_rate", lambda v: f"{v:.1%}" if v is not None else "-"),
        ("out-of-policy overrides", "model_out_of_policy", str),
        ("adjudicator fallbacks", "adjudicator_fallbacks", str),
    ]
    w = 30
    typer.echo("  " + "metric".ljust(w) + "".join(c.rjust(16) for c in cols))
    typer.echo("  " + "-" * (w + 16 * len(cols)))
    for label, key, fmt in rows:
        line = "  " + label.ljust(w)
        for c in cols:
            v = res[c].get(key)
            line += (fmt(v) if v is not None else "-").rjust(16)
        typer.echo(line)

    if payload.get("comparison"):
        typer.secho("\nLift over noop", fg="green", bold=True)
        for arm, c in payload["comparison"].items():
            _echo_kv(f"{arm} rupees preserved", fmt_inr(c["rupees_preserved_lift_paise"]))
            _echo_kv(f"{arm} net of false-intervention cost", fmt_inr(c["net_rupees_paise"]))

    if "agent_column" in payload["HONESTY"]:
        typer.secho("\n" + payload["HONESTY"]["agent_column"], fg="yellow")
    typer.secho(f"\nWrote {rd}/eval.json", fg="green")


@app.command()
def report(run: str = typer.Option("demo")) -> None:
    """Render the self-contained HTML report."""
    from grace.evaluation.report import render

    rd = _run_dir(run)
    path = render(rd)
    typer.secho(f"Wrote {path}", fg="green")


@app.command()
def audit(
    mandate_id: str = typer.Argument(...),
    run: str = typer.Option("demo"),
    arm: str = typer.Option("agent"),
) -> None:
    """Print the full decision timeline for one mandate."""
    from grace.orchestrator import arm_db_path
    from grace.store import Store

    s = Store(arm_db_path(_run_dir(run), arm))
    try:
        m = s.get_mandate(mandate_id)
        if m is None:
            typer.secho(f"No mandate {mandate_id} in arm '{arm}'.", fg="red")
            raise typer.Exit(1)
        c = s.get_customer(m.customer_id)
        if c is None:
            typer.secho(f"Mandate {mandate_id} has no customer record.", fg="red")
            raise typer.Exit(1)
        t = s.get_truth(mandate_id)
        typer.secho(f"\n{mandate_id}  [{arm}]", fg="cyan", bold=True)
        _echo_kv("rail / status", f"{m.rail.value} / {m.status.value}")
        _echo_kv("amount", fmt_inr(m.plan_amount_paise))
        _echo_kv("bank / salary day", f"{c.bank} / {c.salary_day}")
        _echo_kv("paid / total", f"{m.paid_count}/{m.total_count}")
        _echo_kv("last error", m.last_error_reason or "-")
        if t:
            typer.secho("  (ground truth, never shown to the adjudicator)", fg="yellow")
            _echo_kv("  truth cause", t.cause.value)
            _echo_kv("  truth at-risk", f"{t.will_fail} ({t.at_risk_reason})")
        typer.secho("\nEvents", fg="cyan")
        for e in s.events_for(mandate_id)[-10:]:
            typer.echo(f"  {e.at.date()}  {e.name:26} {e.error_code or ''}")
        typer.secho("\nDecision trail", fg="cyan")
        for rec in s.audit_for(mandate_id):
            if rec["phase"] == "intent":
                typer.echo(f"  [{rec['phase']}] trigger={rec['trigger']} p_fail={rec['p_fail']}")
                typer.echo(f"     cause={rec['cause']} ({rec['cause_conf']:.2f})  "
                           f"proposed={rec['proposed_action']} -> final={rec['final_action']}")
                if rec.get("gate_flags"):
                    typer.secho(f"     OVERRIDE {rec['gate_flags']}", fg="yellow")
                typer.echo(f"     rationale: {rec['rationale']}")
                if rec.get("evidence_used"):
                    typer.echo(f"     evidence: {rec['evidence_used']}")
                if rec.get("customer_message"):
                    typer.echo(f"     to customer: {rec['customer_message']}")
            elif rec["phase"] == "result":
                typer.echo(f"  [{rec['phase']}] executed={rec['executed']} "
                           f"status_after={rec['status_after']} error={rec.get('error')}")
                if rec.get("api_request"):
                    typer.echo(f"     api: {rec['api_request'].get('method')} {rec['api_request'].get('path')}")
            else:
                typer.secho(f"  [{rec['phase']}] {rec}", fg="red")
    finally:
        s.close()


@app.command()
def intent(
    mandate_id: str = typer.Argument(...),
    text: str = typer.Argument(...),
    run: str = typer.Option("demo"),
    arm: str = typer.Option("agent"),
    offline: bool = typer.Option(True, "--offline/--online"),
    fresh: bool = typer.Option(False, "--fresh",
                               help="Run against pristine seed state, ignoring any batch actions "
                                    "already applied to this arm."),
) -> None:
    """Convert a cancellation intent into a middle-state offer."""
    import shutil

    from grace.intent.converter import convert
    from grace.orchestrator import arm_db_path

    rd = _run_dir(run)
    if fresh:
        dst = arm_db_path(rd, "intentdemo")
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(dst) + suffix)
            if p.exists():
                p.unlink()
        shutil.copy2(rd / "grace.db", dst)
        arm = "intentdemo"

    res = convert(rd, arm, mandate_id, text, offline=offline,
                  today=DEFAULT_DECISION_DATE)
    typer.secho(f"\n{mandate_id}: \"{text}\"", fg="cyan", bold=True)
    _echo_kv("cause", f"{res['cause']} ({res['cause_confidence']:.2f})")
    _echo_kv("proposed", res["proposed_action"])
    _echo_kv("after policy", res["final_action"])
    if res.get("gate_flags"):
        typer.secho(f"  OVERRIDE {res['gate_flags']}", fg="yellow")
    _echo_kv("offer", res.get("offer") or "-")
    _echo_kv("rationale", res["rationale"])
    if res.get("customer_message"):
        _echo_kv("to customer", res["customer_message"])


@app.command()
def approve(
    decision_id: str = typer.Argument(...),
    run: str = typer.Option("demo"),
    arm: str = typer.Option("agent"),
    by: str = typer.Option("ops", help="Who approved."),
) -> None:
    """Human approval for an escalated action (e.g. request_reauth)."""
    from grace.orchestrator import arm_db_path
    from grace.store import Store

    s = Store(arm_db_path(_run_dir(run), arm))
    try:
        d = s.get_decision(decision_id)
        if not d:
            typer.secho(f"No decision {decision_id}", fg="red")
            raise typer.Exit(1)
        s.append_audit(phase="approval", decision_id=decision_id,
                       mandate_id=d.get("mandate_id") or decision_id.rsplit(":", maxsplit=1)[-1],
                       human_approved_by=by, approved_action=d["proposed_action"])
        typer.secho(f"Recorded approval of {d['proposed_action']} by {by}.", fg="green")
        typer.secho("Execution of approved re-auth is out of scope for this build "
                    "(it creates a NEW mandate and needs the customer).", fg="yellow")
    finally:
        s.close()


@app.command("live-demo")
def live_demo(
    amount: int = typer.Option(49900, help="Plan amount in paise."),
    keep: bool = typer.Option(False, help="Do not cancel the created subscription."),
) -> None:
    """Prove pause/resume/cancel against the REAL Razorpay test-mode API."""
    from grace.rzp.live import live_demo as run_live

    run_live(amount_paise=amount, keep=keep, echo=typer.echo)


@app.command()
def serve(port: int = typer.Option(8000), run: str = typer.Option("demo")) -> None:
    """Run the webhook receiver, intent endpoint and report server."""
    import os

    import uvicorn

    os.environ["GRACE_RUN"] = run
    uvicorn.run("grace.app:app", host="127.0.0.1", port=port, log_level="info")


@app.command("check-llm")
def check_llm(
    run: str = typer.Option("demo"),
    mandate_id: str = typer.Option(None, help="Which mandate to adjudicate (default: first at-risk)."),
    effort: str = typer.Option(None, help="minimal | low | medium | high."),
    model: str = typer.Option(None, help="Pin exactly this model: no fallback chain, so a "
                                          "failure is reported rather than served elsewhere."),
) -> None:
    """One real LLM call on one mandate. The cheapest way to prove the online path."""

    from grace.adjudicate import credentials_present, make_llm_adjudicator, sdk_present
    from grace.evidence import build_evidence
    from grace.orchestrator import arm_db_path
    from grace.predict.features import featurise
    from grace.predict.risk import LogisticRisk
    from grace.signals.bank_health import BankHealth
    from grace.signals.holidays import HolidayCalendar
    from grace.store import Store

    for ok, why in (sdk_present(), credentials_present()):
        if not ok:
            typer.secho(why, fg="red")
            raise typer.Exit(2)

    rd = _run_dir(run)
    db = arm_db_path(rd, "agent")
    if not db.exists():
        db = rd / "grace.db"
    if not db.exists():
        typer.secho(f"No cohort at {rd}. Run `grace seed --run {run}` first.", fg="red")
        raise typer.Exit(1)

    s = Store(db)
    try:
        target = None
        if mandate_id:
            target = s.get_mandate(mandate_id)
        else:
            for m in s.all_mandates():
                t = s.get_truth(m.id)
                if t and t.will_fail:
                    target = m
                    break
        if target is None:
            typer.secho("No suitable mandate found.", fg="red")
            raise typer.Exit(1)

        bh, cal = BankHealth(), HolidayCalendar()
        t = s.get_truth(target.id)
        ev0 = build_evidence(s, target, bank_health=bh, calendar=cal,
                             today=DEFAULT_DECISION_DATE,
                             cancel_intent_text=t.cancel_intent_text if t else None)
        w = rd / "risk_weights.json"
        p, tau = 0.0, 0.60
        if w.exists():
            mdl = LogisticRisk.load(w)
            p, tau = mdl.predict(featurise(ev0)), mdl.preemptive_threshold
        ev = ev0.model_copy(update={"p_fail": p, "preemptive_threshold": tau})
    finally:
        s.close()

    adj = make_llm_adjudicator(effort=effort, model=model)
    chain = getattr(adj, "model_chain", [adj.model])
    typer.secho(f"Calling {adj.name} (effort={adj.effort}) on {target.id}", fg="cyan")
    typer.secho(f"  chain: {' -> '.join(chain)}", fg="cyan")
    d = adj.decide(ev)
    meta = adj.metas[-1] if adj.metas else {}

    typer.secho("\nDecision", fg="green", bold=True)
    _echo_kv("cause", f"{d.cause.value} ({d.cause_confidence:.2f})")
    _echo_kv("action", f"{d.action.value} ({d.action_confidence:.2f})")
    _echo_kv("escalate", d.escalate)
    _echo_kv("rationale", d.rationale)
    _echo_kv("evidence used", d.evidence_used)
    if d.customer_message:
        _echo_kv("to customer", d.customer_message)
    typer.secho("\nCall", fg="green", bold=True)
    for k in ("model", "requested_model", "fallback_depth", "thinking_level",
              "input_tokens", "output_tokens", "thinking_tokens",
              "cache_read_input_tokens", "latency_ms", "request_id"):
        if k in meta:
            _echo_kv(k, meta[k])
    if meta.get("fallback_depth"):
        typer.secho(f"  NOTE: {meta['requested_model']} was unavailable; served by "
                    f"{meta['model']}.", fg="yellow")
    if t:
        typer.secho(f"\n(ground truth for this mandate: {t.cause.value}, "
                    f"at_risk={t.will_fail})", fg="yellow")
    typer.secho("\nOnline path verified.", fg="green", bold=True)


@app.command("day1")
def day1(
    wait: int = typer.Option(0, help="Seconds to wait while you authorise the subscription."),
    amount: int = typer.Option(49900, help="Plan amount in paise."),
    keep: bool = typer.Option(False, help="Do not cancel the created subscription."),
) -> None:
    """Run the Day-1 live API checks against Razorpay test mode."""
    from grace.rzp.day1 import main

    argv = ["--wait", str(wait), "--amount", str(amount)] + (["--keep"] if keep else [])
    raise typer.Exit(main(argv))


if __name__ == "__main__":
    app()
