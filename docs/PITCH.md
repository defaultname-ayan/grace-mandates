# 5-minute pitch — shot list

Record with a terminal on the left and `runs/demo/report.html` on the right. Every number below
comes from a real run of this repo; re-run `grace seed && grace run-batch --offline && grace eval`
before recording so the figures on screen match.

| t | Screen | Say |
|---|---|---|
| **0:00** | Fix My Itch entry, its scores highlighted | "Razorpay's own crowdsourced database ranks this the number one payments problem — severity 9, frequency 9, whitespace 8.5. I checked it against NPCI and against Razorpay's own docs, and the problem statement is wrong in an interesting way." |
| **0:30** | The correction table in README | "Pause *does* exist for UPI Autopay — inside the customer's UPI app, where the merchant can't reach it. And Razorpay's docs say if the customer pauses, only the customer can resume. For eMandate the customer can't pause at all. And on both rails the merchant can't change amount or date — cards only. So pause is the only graceful lever a merchant has, and nothing wires it to the moments that matter." |
| **1:10** | `grace/policy/actions.py` on screen | "Here's that constraint as code, not as a prompt. And here's the one that shapes the whole product: a PENDING subscription cannot be paused. Razorpay's own Subscription Recovery agent starts at `subscription.pending` — which is precisely the state where the graceful lever is already gone." |
| **1:40** | `grace run-batch --run demo --offline` streaming | "Two thousand synthetic mandates, six months of history from a state machine that reproduces Razorpay's documented transitions and retry ladder. Labelled synthetic everywhere — here's the manifest with every parameter." |
| **2:10** | `grace audit <pre-debit mandate>` | "Case A, and this is the one that matters. No failure yet. The debit is scheduled three days before this customer's salary, and they've failed before. Grace pauses one cycle and schedules resume two business days after salary. It could only do that because the mandate is still ACTIVE." |
| **2:40** | `grace intent … "travelling for 2 months"` | "Case B: a cancellation message becomes a two-cycle pause instead of a termination. On eMandate that's the only offer Razorpay permits — and cancelling would force re-registration, which per NPCI fails at a rate that has doubled since 2017." |
| **3:05** | Audit row showing `integrity_blocked` | "Case C. The baseline does the natural naive thing — the payment failed, charge it again — and walks into the eMandate confirmation window seventeen times. Razorpay's own docs say that confirmation can take more than 24 hours. The guard blocks every one." |
| **3:25** | `grace run-batch --no-guard --arms baseline` | "Same batch, guard off. Those become real double debits. Grace never auto-refunds one — it opens a ticket and escalates. A double debit is a compliance incident, not a missed optimisation." |
| **3:45** | Audit row with `model_out_of_policy` / `human_required` | "Case D: thin evidence, or an action outside what the rail permits. The model proposes; code disposes. Every override is counted in the report — nothing is silent." |
| **4:05** | Report results table | "Holdout only. Against doing nothing: 24 mandates preserved versus 15. Against the rules baseline I'd have written without an LLM — run through the *same* guardrails — 24 versus 20, cause accuracy 83% versus 59%, regret down a third." |
| **4:25** | Point at the false-intervention row | "And here's what it cost. Thirty percent of the agent's interventions landed on mandates that would have paid anyway. The baseline's is zero, because it only reacts to failures that already happened. Net of that harm the agent is still ahead — seventy-three thousand against fifty-two — but I'd rather show you the number than hide it." |
| **4:45** | `docs/WHAT-BROKE.md` | "What broke: my holdout leaked because Python's hash is per-process randomised. Cohort generation took two minutes until I found I was doing eighteen thousand fsyncs. And the product's core claim was unreachable for a while — every at-risk mandate was already PENDING, so the pre-emptive pause could never fire. The metric is what caught it." |

## Closing line

> "Razorpay shipped mandate *cancellation* APIs at Sprint'26. Grace is the counterweight — the state
> between debit and gone."

## Before recording

```bash
.venv/bin/python -m pytest tests/ -q        # 106 passing
rm -rf runs/demo
.venv/bin/grace seed --n 2000 --run demo
.venv/bin/grace run-batch --run demo --offline
.venv/bin/grace eval --run demo
.venv/bin/grace report --run demo
```

Pick the demo mandates first — they change with the seed:

```bash
.venv/bin/python -m scripts.pick_demo_cases --run demo
```

## Say this if asked "why not just rules?"

Point at `tests/test_adjudicator.py`, cases `remap_in_flight_card` and `genuine_expiry_new_customer`.
Identical reason code — `card_expired` — opposite correct actions, decided by tenure and payment
history. Then note that NPCI rewrote the NACH reason codes in January 2025: 20 added, 33 revised,
22 removed. A reason→action lookup table goes stale by design.

Then be honest: the rules baseline in the results table does respectably. The argument is not that
rules cannot decide; it is that the vocabulary they decide against keeps moving.
