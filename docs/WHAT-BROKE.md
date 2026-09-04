# What broke, and how it was fixed

A running log kept during the build. Razorpay's brief asks what broke and how it was solved; these
are the real ones, in the order they were hit.

## 1. The holdout split leaked between runs

The spec used `hash(mandate_id) % 10 < 3`. Python randomises string hashing per process
(`PYTHONHASHSEED`), so the training run and the evaluation run would have drawn **different**
holdouts — the model would have been evaluated partly on its own training data, silently, with no
error and a suspiciously good score.

Caught before writing the split, by asking why the split needed to be stable across processes.
Fixed with `sha256`. `test_holdout_split_is_stable_across_processes` locks it.

## 2. Generating the cohort took over two minutes; then four seconds

`grace seed --n 2000` ran past a 120-second timeout. Profiling against `/tmp` showed 2 seconds — the
difference was that `/tmp` is tmpfs and `runs/` is real disk. `Store` committed on **every** write,
so an 18,000-event cohort meant 18,000 fsyncs.

Fixed with `PRAGMA synchronous=NORMAL` (durable across application crashes; only an OS crash can
lose the tail) and a `Store.bulk()` context manager that defers commits during generation.
**124s → 4.2s.**

## 3. `pkill -f "grace seed"` killed the shell that ran it

The pattern matched the invoking shell's own command line, which contained the string. The whole
command died before the follow-up work ran. Replaced with an explicit PID sweep that excludes the
current process. A small thing, but it cost a cycle and it is exactly the kind of self-match that
looks like a hang.

## 4. The integrity guard never fired — because the gate checked the wrong thing first

`guard blocks: 0` across the whole batch. Two causes:

- **Ordering.** The gate checked the salary constraint before the integrity guard, so a charge that
  would have double-debited was rejected for the *wrong reason* ("salary is 3 days away") and the
  guard never ran. Reordered: **integrity before efficacy**. A double debit is a compliance
  incident; a mistimed charge is a wasted attempt.
- **Over-broad rule.** The spec applies the salary constraint to every `manual_charge`. Salary
  timing is irrelevant to a *technical* decline. Scoped it to liquidity causes.

Also: nothing in the system ever proposed charging an in-flight invoice, so there was nothing to
block. The realistic naive rule — *the payment failed, charge it again* — is exactly what produces
double debits in production, so the rules baseline now does that, in-flight-blind, as a real
comparator. Guard blocks went 0 → 17, and `--no-guard` produces real double debits.

## 5. The product's core claim was unreachable by construction

The batch showed **zero** pre-emptive interventions, and of 1,019 mandates reaching the "predicted"
trigger, **zero were actually at risk**.

The cohort generator applied every payment failure *immediately*, so an at-risk mandate was always
already `PENDING` at the decision point. But Razorpay does not permit pausing a `PENDING`
subscription — so the pre-emptive pause, which is the entire differentiator against Razorpay's
post-failure Subscription Recovery agent, could never happen. The demo would have shown a product
that cannot do the thing it claims.

Fixed by modelling what is actually true: Grace runs daily, so it meets a doomed mandate on either
side of its debit date. 45% of at-risk mandates are now **pre-debit** — still `ACTIVE`, still
pausable. Pre-emptive interventions went 0 → 83.

## 6. The predictor was worse than guessing the base rate

After fix #5, holdout Brier was 0.156 — against 0.108 for simply predicting the 12.3% base rate. The
model was actively harmful as a probability.

Cause: class weighting. It improves ranking on an imbalanced cohort but pushes predictions toward
0.5 and destroys calibration — and a miscalibrated `p_fail` is useless as an action threshold.
Removed it (**Brier 0.0802**, now genuinely better than base rate), and instead selected the
pre-emptive-action threshold on the **training split** by maximising counterfactual net value.

## 7. The false-intervention metric was measuring the wrong thing

`Truth.will_fail` initially meant "the debit fails". Under that definition, converting a cancel
intent into a pause counted as a **false** intervention — the headline metric was inverted for the
product's best case.

Split into `will_fail` ("at risk of being lost this cycle", the label the predictor targets and the
metric uses) and `payment_will_fail` ("the debit itself fails"). Cancel intents are at risk without
any payment failure.

## 8. Tightening the pre-emptive rule was free

The first working agent had a 60% false-intervention rate: it paused any mandate with salary nearby
whenever `p_fail ≥ 0.35`, so the predictor's false positives flowed straight into customer-visible
action. Requiring **both** a high calibrated risk and at least one prior failure cut it to 20% with
**no loss in preservation**, and improved action regret. Worth stating plainly: the first version
was worse than the second in every respect, and the metric is what showed it.

## 9. Switching provider surfaced two Gemini-3 traps

Moving the default from Claude to Gemini, verified against `google-genai` 2.22.0 by introspection
rather than recall:

- **`thinking_level` vs `thinking_budget`.** Gemini 3 replaced the budget parameter with a level
  (`minimal|low|medium|high`). The legacy one still works, but **sending both is a 400.** Grace
  sends only `thinking_level`, mapped from its own effort setting.
- **Temperature.** The instinct is to lower it for a decision engine. Google's Gemini 3 guidance is
  the opposite: keep it at the default 1.0, because lowering it can cause looping and degrade
  reasoning on hard tasks. Grace leaves it unset. Determinism comes from the policy gate, not from
  sampling.

Also worth recording, because it changes what the credential pre-check is *for*: `google-genai`
**raises at construction** without a key, while the Anthropic client does not. So a Gemini run fails
fast on its own; an unchecked Anthropic run would have fallen back to escalate on every mandate and
still exited 0 looking successful. The pre-check exists for the second case.

The flattened `Decision` schema paid off here: it converted to Gemini's schema format with all 12
properties and both enums intact, first try. Nested optional objects and tight JSON-Schema
constraints are the two things most likely to fail in structured-output conversion, and neither was
present.

## 10. Razorpay Subscriptions is 401 on a fresh test account — the simulator's whole justification, confirmed

Ran `grace day1` against a real Razorpay test account with freshly generated
test-mode keys. It failed, and the way it failed is the most useful result in this file.

The credentials are fine. With the *same key*:

| Endpoint | Result |
|---|---|
| `GET /v1/payments` | **200** |
| `GET /v1/orders` | **200** |
| `GET /v1/invoices` | **200** |
| `GET /v1/settlements` | **200** |
| `GET /v1/plans` | **401 Unauthorized** |
| `GET /v1/subscriptions` | **401 Unauthorized** |
| `POST /v1/plans` | **401 Unauthorized** |

Reads *and* writes on the Subscriptions API are refused while every other core
product answers normally. Clicking through the Subscriptions onboarding in the
dashboard ("Skip And Get Started") did not change it. Razorpay gates the
Subscriptions API behind **full account activation (KYC)**, which takes 24-48
hours and cannot be self-served — the dashboard says as much: *"You are in Test
Mode... Activate your account to start making live transactions."*

**So the entire product surface Grace is built on is unreachable from a fresh
test account.** Every interesting decision — the retry ladder, the pause-only-
from-ACTIVE rule, the eMandate confirmation race — is unexercisable live until
Razorpay approves the account. This is the strongest possible argument for the
simulator: it is not a convenience, it is the only way to build against
Subscriptions before activation clears.

Two fixes came out of it:

- `grace day1` used to surface this as a bare `ServerError` traceback from deep
  inside the razorpay SDK, with the message swallowed. It now probes core vs
  subscriptions endpoints first and reports the diagnosis in one line.
- `LiveClient` gained `SubscriptionsNotEnabled`, so the condition is a named,
  catchable state rather than an opaque 401.

**Still open, and honestly so:** questions 1-3 below cannot be answered until
the account activates. The simulator's assumptions about them are documented
and unverified.

## 11. Gemini free-tier quota is the real constraint on an online batch

The online path works — `grace check-llm` returned a correct decision on a real
mandate, in Hinglish because the customer's message was in Hinglish. Scaling it
up is what broke.

- `gemini-3.8-flash` returned `503 UNAVAILABLE - this model is currently
  experiencing high demand`, intermittently, and no backoff fixes an overloaded
  model. Hence the fallback chain.
- After roughly one batch's worth of traffic, the whole flash tier
  (3.8 / 3.7 / 3.5) returned **`429 RESOURCE_EXHAUSTED`** — the free-tier daily
  quota is shared across them. Only the *lite* models still served.
- Measured sustained throughput on the lite tier was **under ~7 successful
  requests per minute**, so a 380-call holdout batch is an hour-plus and kept
  stalling.
- Two concurrent workers still rate-limited each other into constant backoff;
  the earlier four made it worse, not better.

Rather than fake a full online run or quietly report a truncated one, `--sample N`
takes a **deterministic** subset of triggered holdout mandates and `grace eval
--on-sample` scores *every* arm on exactly that subset, so the comparison stays
paired. The full-holdout table stays the offline one, which is complete and
reproducible.

A related bug this exposed: progress was only printed after the whole thread
pool drained, so a 40-minute online run looked frozen. It now reports as each
call lands.

## 12. A review pass found eleven more, three of them in the numbers

A static pass (ruff bugbear, mypy) plus an adversarial read-through. In order of how much they
mattered:

- **The simulator dated the "current" cycle into last month.** For 7 of 9 cycle days (any day after
  the 4th), a failing mandate's ladder was anchored to the *previous* month's debit date, duplicating
  the final history cycle with a retry scheduled ~27 days in the past. The adjudicator reasoned about a
  "scheduled retry after salary" that was stale, and the guard's 24h-retry rule could never fire.
  `open_current_cycle` is now calendar-honest: this month's debit date, pre-debit if it is still
  ahead, retry ladder anchored to the actual attempt otherwise. Cycle days now weight toward the
  first week of the month (salary-aligned), which is both more realistic and gives the post-attempt
  population enough mass to exercise the guard.
- **`rupees_at_stake` was recomputed after the action.** A step-down cuts the plan to 60%, and the
  decision record's stake was read from the *post-action* mandate, so every step-down mandate was
  under-counted in `rupees_preserved`. Frozen before execution; `Mandate.rupees_at_stake` is now the
  single definition (it was written out four times).
- **`SHIFT_START` was unreachable.** It is only ever legal on `AUTHENTICATED` subscriptions, where
  `paid_count` is 0 by definition — which the relationship gate then denied. The prompt advertised a
  capability the policy could never approve. Pre-relationship actions are now exempt.
- **`prior_fail_count_6m` counted attempts, not cycles.** The retry ladder emits one event per attempt,
  so one current failure with two retries read as "failed twice before" — the exact signal that decides
  whether a `card_expired` is a reissue remap or a dead card. The remap rule could never fire. Counted
  per invoice now, excluding the cycle under decision.
- **The guard's check took the lock as a side effect.** A later gate step (salary timing) could still
  deny, leaving the invoice locked for 72h; the next charge on it was refused as "already in progress".
  The check is pure; the executor acquires and releases.
- **The Razorpay probe bypassed the test-key guard.** `subscriptions_enabled` re-read the env and made
  raw HTTP calls, so a live key in the environment would have reached the API even though
  `LiveClient()` refuses to construct with one. It now probes through the guarded client.
- **`data/` lived outside the package.** `pip install -e .` worked; a plain `pip install .` silently lost
  bank-health, holiday and NACH files. Moved to `grace/data/`, shipped as package data.
- Two unrelated exception classes both named `AdjudicationError` (one per provider); merged into
  `adjudicate/base.py` with the shared retry/backoff/decide plumbing.
- A `--model` pin still fell through the fallback chain, so `check-llm --model X` could report a
  different model as "verified". Pinned means pinned now, and `GRACE_MODEL_FALLBACKS=` (set, empty)
  means no fallbacks. Also: a 429 with a fallback available no longer sleeps on the dead model, and a
  model that exhausts retries goes into a 120s cooldown instead of being re-probed per mandate.
- `run-batch` committed SQLite after every write; step 3 now runs in one `bulk()` transaction.
- `cohort.py` drove `store.bulk()` by hand with no `try/finally`; an exception mid-generation would have
  left every later write uncommitted. Also fixed: the dotenv loader ignored `export` and inline
  comments; `scripts/day1_live_checks.py` never imported `config` so `.env` was never loaded when run
  as a script; webhook handler 500'd on a missing run DB, a non-ASCII signature or malformed JSON;
  `converter(execute_now=True)` bypassed the stopping rules; `engine.manual_charge` had no status or
  ownership guard; `SimClient` stubs used per-process `hash()`.

Each of these has a regression test. 160+ tests now.

## 13. The online run kept being shaped by quota, not by the model

Three attempts at a scored online run, and every one was decided by the provider rather than by
anything in this repo.

- Attempt 1 (60-mandate sample) completed on a cohort that still had the current-cycle dating bug,
  so it could not stand.
- Attempt 2, after the fix, got **16 of 60** before the free-tier daily quota ran out.
- Attempt 3, on a fresh key, covered the whole triggered holdout: **81 of 137 got a real decision,
  56 hit the quota wall.** A probe minutes earlier had shown the flash tier answering in 4.5s; under
  sustained load it 503'd and 429'd, and the chain fell through -- only 4 of 81 calls came from the
  requested `gemini-3.7-flash`, 58 from `gemini-3.1-flash-lite`.

Two things follow, and both are in the README.

**All 56 quota failures escalated. None acted.** That is the fallback doing exactly what it exists
for, and it is the strongest evidence in the repo that the safety property holds under a real
provider outage rather than a simulated one.

**Scoring them would have been dishonest.** Counting 56 forced escalations as the agent's decisions
drags its measured performance down for a reason that has nothing to do with its judgement -- and
the reverse framing, quietly dropping them, would flatter it. So `grace eval --on-model-decided`
scores *every* arm on the mandates the model actually decided, and the restriction is recorded in
`eval.json` with the count that hit the wall. On that basis the model **loses to the rules baseline
on rupees preserved** and wins on cause accuracy; the README leads with the loss.

---

## Day-1 live API checks — BLOCKED ON ACCOUNT ACTIVATION

`scripts/day1_live_checks.py` **has now been run** against a real test account. It got as far as
the credential and product-access probe and stopped there: Subscriptions is not enabled (see #10).
Re-run `grace day1` once the account activates. It answers:

1. Does `resume` keep the original cycle date or reschedule? (decides whether pause+resume can
   emulate a date shift on UPI/eMandate)
2. What value does `pause_initiated_by` take for a **customer** pause? Only `"self"` is documented.
3. Is pause enabled by default on a fresh test account, or does it return
   `"pause is not allowed, feature is not enabled"`?
4. Does the retry ladder shift `charge_at` exactly +1 day per failure, and halt on the 4th?

The simulator encodes the documented answer to (4) and assumes for (1) that resume reschedules to
the next cycle boundary at or after the resume date. **If the live check contradicts that, the
simulator is wrong and this file should say so.**

Likewise **no live LLM call has been made** — no `GEMINI_API_KEY` was available. Request shape,
thinking level, schema conversion, refusal/truncation detection, retry policy and clamping are
verified against a fake SDK (`tests/test_gemini_adjudicator.py`, 22 tests); the model's judgement
quality is unmeasured. `grace check-llm` proves the path in a single call — run it first.
- `2026-09-04 08:10 UTC` Day-1 live checks SKIPPED: no RAZORPAY_KEY_ID in the environment.
- `2026-09-04 08:10 UTC` Day-1 live checks SKIPPED: no RAZORPAY_KEY_ID in the environment.
- `2026-09-04 11:07 UTC` Day-1 check `subscriptions API enabled`: FAIL - Subscriptions is NOT enabled on this account. The same key reads /payments fine (200) but /plans returns 401. Razorpay gates the Subscriptions API behind full account activation (KYC), which takes 24-48h. Everything else in Grace runs on the simulator meanwhile.
