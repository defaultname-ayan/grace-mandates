# Grace — the missing middle state for Indian auto-debit

An Indian recurring mandate has two states that matter: **debit** or **gone**. There is no
negotiable middle, so every failure path collapses to the same terminal outcome — four failed
retries and a halt, or a revocation, or a cancellation the customer can't easily undo because
re-registration frequently fails.

Grace inserts the middle state. At **predicted failure**, **actual failure**, or **cancel intent**,
it chooses a bounded action — pause a cycle, resume after salary, cancel at cycle end instead of
now, charge an existing invoice when that's safe, step down a plan — executes it against Razorpay
Subscriptions, and writes an audit record for every money action.

Built for the **Razorpay AI Buildathon, Track 03 — AI Revenue Recovery**.

---

## The correction that motivates it

Razorpay's own [Fix My Itch](https://razorpay.com/m/fix-my-itch/) database ranks
*"Why isn't subscription pause logic built into auto-debit infrastructure?"* as its highest-scoring
payments problem (severity 9, frequency 9, whitespace 8.5). Its problem statement says:

> *"No temporary freeze option exists for UPI autopay, e-mandates, or standing instructions."*

**That is wrong, in a way that makes the real problem sharper.** From Razorpay's own docs:

| Their claim | What the docs actually say |
|---|---|
| No freeze for UPI Autopay | UPI Autopay **does** support pause — but from the **customer's UPI app**. And *"you cannot resume a Subscription paused by your customer. Only your customer can resume such Subscriptions."* |
| No freeze for e-mandates | True for the **customer**: *"your customer cannot pause or cancel a Subscription that is authorised via Emandate."* The **merchant** can pause via API — but nothing surfaces it at the moment of churn. |
| — | On UPI **and** eMandate the merchant cannot change amount, plan, quantity or date: *"You can only update a Subscription authorised using cards."* |

So on the two rails that carry Indian recurring payments, **pause is the only graceful lever a
merchant has, and it is not wired to the moments that matter.** Grace wires it.

### Why this is not Razorpay's Subscription Recovery agent

| | Razorpay Subscription Recovery (Agent Studio, Mar 2026) | Grace |
|---|---|---|
| Trigger | `subscription.pending` — after a failure | predicted failure · actual failure · **cancel intent** |
| Action space | retry, nudge | pause, resume-on-date, cancel-at-cycle-end, guarded manual charge, step-down |
| Timing | post-failure only | **pre-debit, where pause is still legal** |
| eMandate async | not addressed | double-debit guard |

A `PENDING` subscription **cannot be paused**. That single constraint is why the pre-debit window is
the whole product: it is the only time the graceful lever is available.

---

## Results

Holdout only (30% of the cohort, never used for fitting or threshold selection).
**The cohort is synthetic — see [Honesty](#honesty).**

| Metric | do nothing | rules baseline | agent |
|---|---|---|---|
| Mandates scored (holdout) | 612 | 612 | 612 |
| At risk | 75 | 75 | 75 |
| **Mandates preserved** | 15 | 20 | **24** |
| **Rupees preserved** | Rs 47,655 | Rs 99,540 | **Rs 1,23,829** |
| Preservation rate | 20.0% | 26.7% | 32.0% |
| Interventions | 0 | 31 | 43 |
| False interventions | 0 | 0 | 13 |
| **False-intervention rate** | – | **0.0%** | **30.2%** |
| False-intervention cost | Rs 0 | Rs 0 | Rs 2,837 |
| Escalation rate | 0.0% | 12.7% | 9.1% |
| Cause accuracy | 5.6% | 59.3% | 83.3% |
| Action regret (lower is better) | 0.2152 | 0.1580 | 0.1219 |
| Intent conversion | 0.0% | 37.0% | 85.2% |

**Net of the harm it causes:** the agent preserves Rs 76,174 more than doing nothing and costs
Rs 2,837 in revenue put at risk by intervening on 13 healthy mandates — **net Rs 73,337**, against
the baseline's Rs 51,885.

**Read the false-intervention row before the rupee row.** The agent buys its extra preservation by
acting more often, and 30% of its interventions land on mandates that would have paid anyway. The
rules baseline never does that, because it only ever reacts to a failure that has already happened.
That is the real trade: Grace acts earlier, and acting earlier means sometimes acting wrongly.

**Counterfactual formula.** `rupees preserved = Σ plan_amount × min(3, remaining_cycles)` over
at-risk mandates that survive. Survival is drawn from the cohort's counterfactual table using
**common random numbers** — every mandate gets one fixed uniform shared across all three arms, so
the arms differ only by the action they chose, never by luck.

### Live model result (60-mandate online sample)

The table above ran the deterministic offline stub. Below is a **real Gemini run** on a
deterministic 60-mandate subset of the holdout, with every arm scored on exactly those 60 so the
comparison stays paired. Reproduce with:

```bash
grace run-batch --run demo --online --arms agent --sample 60 --workers 2
grace eval --run demo --on-sample
```

| Metric | do nothing | rules baseline | **agent (Gemini)** |
|---|---|---|---|
| Mandates scored | 60 | 60 | 60 |
| At risk | 30 | 30 | 30 |
| Mandates preserved | 4 | **7** | **7** |
| Rupees preserved | Rs 22,488 | **Rs 68,379** | **Rs 68,379** |
| Interventions | 0 | 12 | **6** |
| False-intervention rate | – | 0.0% | 0.0% |
| Escalation rate | 0.0% | 18.3% | 8.3% |
| Cause accuracy | 0.0% | 59.99% | **73.3%** |
| Action regret (lower better) | 0.2311 | 0.1784 | **0.1759** |

**On this sample the agent does NOT beat the rules baseline on revenue preserved — it ties it.**
Same 7 mandates preserved, same Rs 68,379. What it does differently is get there with **half the
interventions** (6 vs 12), a higher cause accuracy (73.3% vs 60.0%), and less than half the
escalation rate. Same outcome, half the customer contact.

That is a genuinely weaker result than the offline table suggests, and it is the honest one. Three
things to weigh before reading anything into it:

- **n is small.** 60 mandates, 30 at risk. Nowhere near enough to separate two arms on a rupee
  total; the interventions and cause-accuracy gaps are the only differences with any margin.
- **It was served by a lite model.** 59 of 60 calls went to `gemini-3.5-flash-lite` and 1 to
  `gemini-3.1-flash-lite` (the fallback chain firing in production), because free-tier quota for the
  flash tier was exhausted. This is not a `gemini-3.8-flash` result and is not presented as one.
- **Cost:** 109,043 input + 13,196 output + 55,869 thinking tokens, 9.4s mean latency.

The policy layer earned its place here on real model output: `param_rewritten` x3 (dates
re-derived), `human_required` x2, and **`cancel_cause_gate` x1 — the model proposed cancelling a
subscription and code refused it** for insufficient cause confidence.

### Integrity

| | do nothing | baseline | agent |
|---|---|---|---|
| Guard blocks | 0 | 17 | 1 |
| Double debits prevented | 0 | 15 | 0 |
| **Double debits detected** | 0 | **0** | **0** |

The baseline follows the natural naive rule — *the payment failed, so charge it again* — and walks
straight into the eMandate confirmation window 17 times. The guard stops every one. The agent
proposes it once, because it can see `emandate_attempt_in_flight` and reasons about it.

Run `grace run-batch --no-guard` to see the same batch without protection: the blocked charges
become **real double debits**. Grace never auto-refunds one; it opens a ticket and escalates.

### Predictor

Brier **0.0802** on the holdout, against **0.1075** for simply predicting the base rate.
The pre-emptive-pause threshold (0.20) was chosen on the **training split** by maximising
counterfactual net value. The holdout was never consulted for it.

---

## Honesty

- **The cohort is synthetic.** 2,000 mandates, 6 months of generated history, ground truth invented
  by `grace/sim/cohort.py`. Every parameter, prior and counterfactual is printed in
  `runs/demo/cohort_manifest.json` and in the report. No merchant data was used. **No rupee figure
  here is an observed result.**
- **The bank-health feed is not NPCI data.** NPCI publishes real per-bank BD/TD and uptime monthly;
  the page is JS-rendered and this build could not fetch it, so `data/bank_health_SYNTHETIC.csv`
  contains invented figures shaped like it, and the report shows a red banner saying so. The
  fetch path (`try_fetch_npci_latest`) exists and fails gracefully rather than guessing.
- **The agent column above ran the offline stub, not Claude.** The stub is deterministic so the
  pipeline is testable without network. Its intent lexicon was tuned on the same templates that
  generate the cohort, so its 85.2% intent conversion is **circular**. Re-run with
  `grace run-batch --online` and real credentials for a model measurement.
- **Outcomes come from a causal model, not from live payments.** The counterfactual survival table
  is an assumption. It is printed in full in the report so you can disagree with it.
- Failure-rate assumptions (UPI Autopay 8–15%, cards 2–3%) come from a PSP blog, not NPCI.

### What has been verified live, and what has not

**Gemini — verified.** `grace check-llm` makes a real call and returns a correct decision. On a
UPI mandate the customer had already paused, it identified `customer_intent_temporary` (matching
ground truth), chose `noop`, cited the rule that only the customer can resume a customer-paused UPI
mandate, and wrote the customer message in Hinglish because the customer's own message was in
Hinglish. Reproduce with `export GEMINI_API_KEY=... && grace check-llm --run demo`.

**Razorpay — blocked, and the block is itself a finding.** Ran against a real test account with
freshly generated test-mode keys. With the same key, `/payments`, `/orders`, `/invoices` and
`/settlements` all return **200**, while `/plans` and `/subscriptions` return **401 Unauthorized**
for both reads and writes. Razorpay gates the Subscriptions API behind full account activation
(KYC, 24-48h); the dashboard onboarding flow does not lift it. **The entire product surface Grace
is built on is unreachable from a fresh test account** — which is the strongest available argument
that the simulator is a necessity rather than a convenience. Details in
[`docs/WHAT-BROKE.md`](docs/WHAT-BROKE.md) §10. Re-run `grace day1` once the account activates.

**Still unmeasured:** the model's *judgement quality at scale*. Free-tier quota (see below) makes a
full 380-call holdout batch impractical, so the headline table remains the offline one. The three
known-unknowns about `resume`, `pause_initiated_by` and pause availability stay unanswered until
the Razorpay account activates.

### Free-tier quota shapes what an online run can be

Measured, not assumed: `gemini-3.8-flash` returns intermittent `503 UNAVAILABLE`; after roughly one
batch's traffic the whole flash tier returns `429 RESOURCE_EXHAUSTED` (the daily quota is shared);
the lite tier sustains **under ~7 successful requests/minute**. Grace handles this with a model
fallback chain that records `served_by` per call, and `--sample N` for a deterministic online subset
scored paired against every other arm (`grace eval --on-sample`). A run served by fallback models is
reported as such and never presented as a flagship-model result.
- Three open questions are listed in [Known unknowns](#known-unknowns).

---

## Quickstart

```bash
python -m venv .venv && .venv/bin/pip install -e .      # 3 dependencies: pydantic, typer, jinja2
.venv/bin/grace seed --n 2000 --run demo                # ~4s
.venv/bin/grace run-batch --run demo --offline          # ~9s, no network
.venv/bin/grace eval --run demo
.venv/bin/grace report --run demo                       # runs/demo/report.html
```

Everything above runs offline. Optional extras:

```bash
.venv/bin/pip install -e ".[llm]"      # google-genai -> grace run-batch --online
.venv/bin/pip install -e ".[live]"     # razorpay   -> grace live-demo, grace day1
.venv/bin/pip install -e ".[serve]"    # fastapi    -> grace serve
.venv/bin/pip install -e ".[dev]"      # pytest     -> 106 tests, ~0.4s
```

### Seeing one decision

```bash
.venv/bin/grace audit simsub_00007 --run demo
# --fresh runs against pristine seed state, ignoring actions the batch already applied
.venv/bin/grace intent simsub_00007 "travelling for 2 months, dont charge me pls" --run demo --fresh
```

Pick concrete mandate ids for each demo case (they change with the seed):

```bash
.venv/bin/python -m scripts.pick_demo_cases --run demo
```

### The counterfactual

```bash
.venv/bin/grace run-batch --run demo --offline --no-guard --arms baseline
```

---

## How it works

```
Observe   subscription snapshot + event stream + payment error reasons
Signal    bank health (BD/TD, downtime), salary proximity, holiday calendar, tenure
Predict   p_fail for the next cycle (calibrated logistic regression)
Gate      below threshold, with no failure and no intent -> no-op, logged
Adjudicate Claude -> {cause, confidence, action, params, rationale, escalate}
Policy    rail x status matrix, bounds, stopping rules, confidence gates
Integrity double-debit guard on anything that charges
Act       Razorpay API (or simulator), audit record before and after
Verify    next cycle: did the mandate survive?
```

**The model proposes; code disposes.** Every action is validated against the rail/status matrix in
`grace/policy/actions.py` before it can execute. An action outside that matrix is overridden to
`escalate` and counted as `model_out_of_policy` in the report. Nothing is silent.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Provider

Default: **Gemini** `gemini-3.8-flash`, with `thinking_level` mapped from Grace's effort setting and
structured output via `response_schema=Decision`. Two Gemini-3 specifics are handled deliberately
and would otherwise be silent bugs:

- reasoning depth is `thinking_level`, not the legacy `thinking_budget`; **sending both is a 400**,
  so Grace only ever sends `thinking_level`;
- **temperature is left unset.** Google's Gemini 3 guidance is to keep it at the default 1.0;
  lowering it can cause looping and degrade reasoning. Grace's determinism comes from the policy
  layer, not from sampling.

Claude (`claude-opus-5`) is available as an alternate via `GRACE_PROVIDER=anthropic`. Because the
adjudicator only ever *proposes* — the rail matrix, bounds and integrity guard are provider-
independent — swapping providers cannot change what the system is allowed to do. Only the quality
of the proposal changes.

## Why an LLM is the right tool here

Not because a rules engine can't decide — the baseline in the results table *is* a rules engine, and
it does respectably. Because the vocabulary the rules would be written against **moves**:

- NPCI rewrote the NACH rejection codes in Jan 2025 (circular NPCI/2024-25/NACH/006): **20 added,
  33 revised, 22 removed.** A hardcoded reason→action table goes stale by design.
- RBI's April 2026 e-mandate framework introduced **per-cycle customer opt-out** and **card-reissue
  remapping** — states most billing systems do not model at all.
- `payment_failed` is Razorpay's documented catch-all: a reason code that is explicitly not a diagnosis.

The clearest case is in the test suite: `card_expired` on a 30-month customer with a clean history
is probably a reissue remap that resolves itself, and re-authorising churns a good customer;
`card_expired` on a 2-month customer with three prior failures is a dead card. **Same reason code,
opposite correct action.** See `tests/test_adjudicator.py::CASES` — `remap_in_flight_card` vs
`genuine_expiry_new_customer`.

## Known unknowns

1. Does `resume` keep the original cycle date or reschedule it? This decides whether pause+resume
   can emulate a date shift on UPI/eMandate. `grace day1` answers it.
2. What value does `pause_initiated_by` take for a **customer** pause? Only `"self"` is documented.
3. Is manual invoice charge available via API, or dashboard-only in test mode? `LiveClient.charge_invoice`
   raises `NotImplementedError` rather than inventing an endpoint.
4. Is pause enabled by default on a fresh test account? The error string
   `"pause is not allowed, feature is not enabled"` exists, so possibly not.
5. The 55% NACH rejection figure's denominator (registration vs execution) is unconfirmed — cite it
   as "mandates rejected", not "registrations".

## Layout

```
grace/sim/          simulator: state machine, cohort generator, reason vocabularies
grace/signals/      bank health, salary cycle, RBI holiday calendar
grace/predict/      features + calibrated logistic regression + threshold tuning
grace/adjudicate/   schema, prompt, Gemini + Claude clients, deterministic offline stub
grace/policy/       rail x status matrix, bounds, the gate
grace/integrity/    double-debit guard
grace/rzp/          Razorpay client (live + simulated), webhook receiver
grace/evaluation/   rules baseline, metrics, eval driver, HTML report
tests/              107 tests, no network
```

## Licence

MIT.
