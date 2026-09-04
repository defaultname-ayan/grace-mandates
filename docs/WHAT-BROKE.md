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

---

## Day-1 live API checks — NOT YET RUN

`scripts/day1_live_checks.py` is written and import-clean but **has never been run**: no Razorpay
test-mode credentials were available in this environment. Run `grace day1` first. It answers:

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
