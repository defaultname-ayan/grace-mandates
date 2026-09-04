# Architecture

```
                        ┌─────────────────────────────────────────┐
                        │                grace/cli.py             │
                        │  seed · run-batch · eval · report · ...  │
                        └───────────────┬─────────────────────────┘
                                        │
  ┌────────────┐   events  ┌────────────▼────────────┐  evidence  ┌──────────────┐
  │ grace/rzp/ │──────────►│     orchestrator.py     │───────────►│ predict/     │
  │  LiveClient│◄──────────│  per-mandate decision   │◄───────────│ p_fail       │
  │  SimClient │  actions  │          loop           │  p_fail    └──────────────┘
  │  webhooks  │           └───┬──────────────┬──────┘
  └─────┬──────┘               │              │ evidence bundle
        │ raw events           │              ▼
  ┌─────▼──────┐               │      ┌──────────────┐  Decision  ┌──────────────┐
  │ audit/     │◄──────────────┼──────│ adjudicate/  │───────────►│ policy/gate  │
  │ event log  │  every        │      │ Claude  |    │            │ rail matrix  │
  │ audit trail│  decision     │      │ offline stub │            │ bounds       │
  └────────────┘               │      └──────────────┘            └──────┬───────┘
                               │                                         │ allowed
                               │      ┌──────────────┐                   │
                               └─────►│ integrity/   │◄──────────────────┘
                                      │ double-debit │
                                      │ guard        │
                                      └──────────────┘
```

## The trust boundary

**The model proposes; code disposes.** This is the single most important property of the system.

`grace/adjudicate/` returns a `Decision`. It executes nothing. `grace/policy/gate.py` then decides
what — if anything — actually happens:

1. **Rail × status matrix.** Is this action even legal for this rail in this state?
   (`grace/policy/actions.py`, encoding Razorpay's documented capability matrix.)
   A violation is overridden to `escalate` and counted as `model_out_of_policy`.
2. **Human gate.** `request_reauth` always escalates. It creates a new mandate and a new customer ask.
3. **Relationship gate.** No intervention on a mandate that has never successfully paid.
4. **Stopping rules.** Max 1 intervention per cycle, 3 per mandate, then escalate.
5. **Cancel gate.** `cancel_at_cycle_end` requires `cause == customer_intent_done` at ≥0.70 confidence.
6. **Confidence gates.** 0.65 for anything that moves money, 0.55 for pause.
7. **Parameter re-derivation.** Pause cycles are clamped; `resume_on` is re-derived from the salary
   cycle and holiday calendar. The model's dates are treated as suggestions.
8. **Integrity, then efficacy.** For a manual charge the double-debit guard runs *first* — a double
   debit is a compliance incident, a mistimed charge is only a wasted attempt. The salary check runs
   second, and only for liquidity causes: salary timing says nothing about a technical decline.

Every override is recorded as a flag in the audit trail and totalled in the report. There is no
silent correction anywhere in this system.

## Why a simulator

Razorpay test mode cannot force individual decline reasons, its subscription tokens expire in three
days, and it will not manufacture an eMandate confirmation race. None of the interesting decisions
can be exercised live. `grace/sim/engine.py` reproduces the documented state machine, the documented
retry ladder, and the documented rail matrix so the batch can be evaluated; `grace live-demo` then
proves the same actions against the real test-mode API.

Two runs with the same seed produce byte-identical event streams.

## Failure modes and fallbacks

| Failure mode | Detection | Fallback |
|---|---|---|
| Claude unavailable, rate-limited, or refusing | exception / `stop_reason == "refusal"` | `safe_default` → escalate, never act. Batch continues; rate reported as `adjudicator_fallback_rate` |
| Model proposes an out-of-policy action | rail × status matrix in `gate()` | override to `escalate`, flagged and counted |
| Model returns wild values (confidence 9.0, 99 cycles) | `Decision.clamped()` | coerced to valid ranges before policy sees them |
| Webhook duplicate or out-of-order | event id primary key; events read back time-ordered | idempotent no-op; `{"duplicate": true}` |
| Webhook tampered | HMAC-SHA256 over the **raw** body, constant-time compare | 400, nothing written |
| eMandate confirmation arrives after a retry was scheduled | invoice lock + `attempt_in_flight` | manual charge blocked; `double_debits_prevented` |
| A double debit happens anyway | two `subscription.charged` on one invoice | ticket opened in the audit trail, escalated. **Never auto-refunded** |
| Pause not enabled on the account | Razorpay error string | `FeatureNotEnabled` with the exact remediation; run continues on the simulator |
| Customer-paused UPI mandate | `pause_initiated_by` | resume blocked at both the matrix and the guard |
| Card token stale after a long pause | `paused_at` age > 60 days | resume blocked, escalate |
| NPCI feed unavailable | fetch returns nothing | bundled snapshot → clearly-labelled synthetic, red banner in the report |
| One mandate throws inside a batch | per-mandate try/except in the orchestrator | that mandate escalates; the other 1,999 proceed |

## Data flow and the truth boundary

Ground truth (`Truth`) lives in its own SQLite table and is read by exactly two things: the cohort
generator that writes it, and the evaluator that scores against it. `build_evidence()` never touches
it. `tests/test_adjudicator.py::test_evidence_never_leaks_ground_truth` asserts that the serialised
evidence blob contains none of `will_fail`, `survival_under`, `payment_will_fail`, `at_risk_reason`
or `propensity`.

Each arm runs against its **own copy** of the seeded database, because actions mutate state and the
arms must not contaminate each other.

## Evaluation design

- **Holdout only.** 30% split by `sha256(mandate_id)`. The spec used Python's `hash()`, which is
  randomised per process — the split would have differed between the training run and the eval run
  and the holdout would have leaked.
- **Common random numbers.** Each mandate gets one fixed uniform draw shared across all arms, so
  arms differ only by their action, never by luck.
- **A real baseline.** The rules baseline is not a strawman: it knows the rail matrix and falls back
  sensibly. It is reported in every table. If it wins, the README says so.
- **Threshold selection on training data.** The pre-emptive-pause threshold is the argmax of
  counterfactual net value on the training split. The holdout is never consulted.

## Prompt caching

The system prompt is static and carries a `cache_control` breakpoint; volatile content (mandate ids,
timestamps, run ids) lives only in the user turn. `test_system_prompt_carries_nothing_volatile`
enforces this — a single interpolated date in the system prompt would silently destroy the cache
across the whole batch.
