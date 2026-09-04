# 5-minute pitch — script and shot list

Everything below is reproducible from this repo. **Numbers change when the cohort is
regenerated** — re-run the setup, then re-read them off the terminal rather than trusting the
figures typed here.

---

## Before you record

```bash
cd ~/razorpay-buildathon/grace-app
.venv/bin/python -m pytest tests/ -q          # 164 passing
rm -rf runs
.venv/bin/grace seed --n 2000 --run demo
.venv/bin/grace run-batch --run demo --offline
.venv/bin/grace eval --run demo
.venv/bin/grace report --run demo
```

Then dry-run the demo driver once so you know the pacing:

```bash
./scripts/demo.sh --auto     # runs straight through
./scripts/demo.sh            # ENTER between beats — use this while recording
```

`scripts/demo.sh` picks mandate ids **at run time** (`scripts/pick_demo_cases.py`), so it never
references a stale id. Nothing in it calls a paid API.

**Terminal:** ~120x34, 16–18pt, dark background. Two windows: terminal left, `runs/demo/report.html`
right. Zoom the browser to ~125% — the tables need to be readable at 1080p.

---

## The shot list

Target 5:00. The four decisions in beat 4 are the heart of it; if you overrun, cut beat 6, not them.

| t | Screen | Say |
|---|---|---|
| **0:00** | Fix My Itch entry, scores visible | "Razorpay's own crowdsourced problem database ranks *subscription pause* as its number one payments problem. Severity 9, frequency 9, whitespace 8.5. I checked it against NPCI and against Razorpay's own docs — and the problem statement is wrong." |
| **0:25** | README correction table | "Pause *does* exist for UPI Autopay. It lives in the customer's UPI app, where the merchant can't reach it — and Razorpay's docs say if the customer pauses, only the customer can resume. For eMandate the customer can't pause at all. And on both rails the merchant can't change amount or date; that's cards only. So pause is the only graceful lever a merchant has, and nothing wires it to the moments that matter." |
| **0:55** | `grace/policy/actions.py` | "Here's that matrix as code, not as a prompt. And here's the line that shapes the whole product: a PENDING subscription cannot be paused. Razorpay's own Subscription Recovery agent starts at `subscription.pending` — which is exactly the state where the graceful lever is already gone. Grace works the window before that." |
| **1:20** | `grace run-batch` streaming | "Two thousand synthetic mandates, six months of history from a state machine that reproduces Razorpay's documented transitions, retry ladder and rail matrix. Labelled synthetic everywhere — the manifest prints every parameter and the counterfactual table the whole evaluation rests on." |
| **1:45** | **Case A** — `grace audit <A>` | "This is the product. No failure yet. The debit is scheduled four days before this customer's salary, and this mandate has bounced twice in six months. It's still ACTIVE, so pause is still legal. Grace pauses one cycle and schedules resume two business days after salary. The rationale cites the exact evidence fields it used." |
| **2:20** | **Case B** — `grace audit <B2>` | "An eMandate customer writes 'moving house this month, pause it.' That becomes a one-cycle pause, not a cancellation — and on eMandate a pause is the *only* offer Razorpay permits. Cancelling would force re-registration, which per NPCI data fails at a rate that has doubled since 2017-18." |
| **2:50** | **Case C** — `grace audit <C> --arm baseline` | "The rules baseline does the natural naive thing — the payment failed, so charge it again — and walks straight into the eMandate confirmation window. Razorpay's own retry docs say that confirmation can take more than 24 hours. The guard blocks it. Twenty blocks across the batch." |
| **3:15** | `--no-guard` counterfactual | "Same batch, guard off. Those become real double debits. Grace never auto-refunds one — it opens a ticket and escalates. A double debit is a compliance incident, not a missed optimisation." |
| **3:35** | **Case D** — an escalation | "Thin evidence — Razorpay's catch-all `payment_failed`, which is explicitly not a diagnosis. Escalate rather than guess. Escalation is cheap; a wrong money action isn't. The model proposes; code disposes, and every override is counted." |
| **3:55** | `grace eval` table | "Holdout only, thirty percent never used for fitting or threshold selection. Against doing nothing: 28 mandates preserved versus 17. Against the rules baseline I wrote myself — run through the *same* guardrails — 28 versus 22, cause accuracy 70 versus 52, regret down twenty percent." |
| **4:20** | point at the false-intervention row | "And here's what it cost. Thirty percent of the agent's interventions landed on mandates that would have paid anyway. The baseline's is zero, because it mostly reacts to failures that already happened. That's the real trade: acting earlier means sometimes acting wrongly. Net of that harm it's still ahead — about ₹1.3 lakh against ₹72,000 — but I'd rather show you the number than hide it." |
| **4:40** | `docs/WHAT-BROKE.md` headings | "What broke. My holdout leaked, because Python's string hash is randomised per process. The product's core claim was unreachable twice — once because every at-risk mandate was already PENDING so the pre-emptive pause could never fire, and once because a fix of mine zeroed the prior-failure signal across the whole cohort. Both times the metric caught it. And when I pointed it at a real Razorpay test account, the entire Subscriptions API returned 401 — it's gated behind KYC. Which is exactly why the simulator exists." |

**Closing line:** *"Razorpay shipped mandate cancellation APIs at Sprint'26. Grace is the counterweight — the state between debit and gone."*

---

## Answers to expect

**"Did you run it against anything real?"**
Yes, and both answers are in the repo. `grace check-llm` makes a real Gemini call and returns a
correct decision — on a UPI mandate the customer had already paused, it identified
`customer_intent_temporary`, chose noop, cited the rule that only the customer can resume a
customer-paused UPI mandate, and replied in Hinglish because the customer wrote in Hinglish.
On Razorpay: `grace day1` against a real test account returns **401 on the entire Subscriptions
API** while `/payments`, `/orders`, `/invoices` and `/settlements` all return 200 with the same key.
Subscriptions is gated behind full account activation. That's the strongest argument for the
simulator I could have asked for.

**"Why not just rules?"**
Point at `tests/test_adjudicator.py`, cases `remap_in_flight_card` and
`genuine_expiry_new_customer`. Identical reason code — `card_expired` — opposite correct actions,
decided by tenure and payment history. Then: NPCI rewrote the NACH reason codes in Jan 2025, adding
20, revising 33 and removing 22. A reason→action lookup goes stale by design.
Then be honest: the rules baseline does respectably. The argument isn't that rules can't decide;
it's that the vocabulary they decide against keeps moving.

**"Which model?"**
Gemini 3.8 Flash by default, Claude Opus 5 behind one environment variable. The point is that it
doesn't matter: the adjudicator only *proposes*. The rail matrix, the bounds, the stopping rules and
the double-debit guard are provider-independent and sit downstream, so swapping the model cannot
change what the system is *allowed* to do — only how good the proposal is. Same reason the
deterministic offline stub can stand in for either in CI.

**"Is the agent column a real model result?"**
No, and the report says so in a banner. The scored table is the deterministic offline stub, so the
pipeline is reproducible with no network. Its intent lexicon was tuned on the same templates that
generate the cohort, so its intent-conversion number is circular. A scored *online* sample is
quota-limited on the free tier: the last complete attempt got 16 real decisions before the daily
quota ran out, and the other 44 escalated — none of them acted. That's the fallback behaving
correctly, but 16 decisions is not a measurement.

---

## Don't claim

- Don't call any rupee figure a real merchant result. Every one is synthetic, from a counterfactual
  table printed in full in the report.
- Don't attribute the bank-health numbers to NPCI. They're invented and labelled `SYNTHETIC`;
  the report shows a red banner.
- Don't say the agent beats the baseline "in production". It beats it on a synthetic holdout, using
  a stub adjudicator, with a 30% false-intervention rate the baseline doesn't incur.
- Don't quote the 55% NACH rejection figure as "of registrations" — the denominator is unconfirmed.
  Say "55% of mandates rejected, per NPCI data via FACTLY."

---

## If you have 30 seconds spare

`grace serve` and POST a cancellation to `/cancel-intent` live — it returns the offer JSON with
`reversible: true`. It's the clearest one-shot demonstration that this is a workflow, not a report.
