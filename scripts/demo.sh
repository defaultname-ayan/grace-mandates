#!/usr/bin/env bash
# Grace - screen-recording demo driver.
#
#   ./scripts/demo.sh          step through beats, ENTER to advance (for recording)
#   ./scripts/demo.sh --auto   run straight through with pauses (for a dry run)
#   ./scripts/demo.sh --list   just print the beats and exit
#
# Mandate ids are SELECTED AT RUN TIME, never hardcoded: they change whenever the
# cohort is regenerated, and a demo that references a stale id is worse than no
# demo. Everything here is offline; nothing calls a paid API.
set -uo pipefail
cd "$(dirname "$0")/.." || exit 1

GRACE=".venv/bin/grace"; PY=".venv/bin/python"
[ -x "$GRACE" ] || { echo "run from the repo root with .venv present"; exit 1; }
AUTO=0; [ "${1:-}" = "--auto" ] && AUTO=1
B=$'\033[1m'; C=$'\033[36m'; Y=$'\033[33m'; G=$'\033[32m'; R=$'\033[31m'; N=$'\033[0m'

beat() { printf '\n%s\n%s  %s%s\n%s\n' "${C}────────────────────────────────────────────────────────────${N}" \
  "${B}${C}" "$*" "${N}" "${C}────────────────────────────────────────────────────────────${N}"; }
say()  { printf '%s» %s%s\n' "$Y" "$*" "$N"; }
run()  { printf '%s$ %s%s\n' "$G" "$*" "$N"; eval "$@"; }
wait_() { if [ "$AUTO" = 1 ]; then sleep "${1:-4}"; else printf '\n%s[ENTER]%s' "$B" "$N"; read -r _; fi; }

if [ "${1:-}" = "--list" ]; then
  grep -n '^beat ' "$0" | sed 's/^\([0-9]*\):beat /  \1  /'; exit 0
fi

# Pick real ids from the current run. Empty is reported, never faked.
# Called AFTER the seed + batch below, because `grace seed` regenerates the
# cohort; ids picked before it would be stale by the time they are audited.
# (The seed is deterministic, so re-running it reproduces the same cohort.)
pick() { $PY -m scripts.pick_demo_cases --run demo 2>/dev/null \
         | awk -v want="$1" '$0 ~ "^CASE "want" " {f=1;next} /^CASE /{f=0} f && /^  simsub_/{print $1; exit}'; }

beat "1/7  The problem, in Razorpay's own words"
say "Their Fix My Itch database ranks subscription pause as its #1 payments problem."
say "Severity 9, frequency 9, whitespace 8.5. And the problem statement is wrong."
run "sed -n '/### The correction/,/^So on the two rails/p' README.md | head -14"
wait_ 8

beat "2/7  The constraint that makes it a product"
say "A PENDING subscription cannot be paused. Razorpay's own recovery agent starts"
say "at subscription.pending - which is exactly when the graceful lever is gone."
run "sed -n '/^def allowed_actions/,/^    if status in/p' grace/policy/actions.py"
wait_ 8

beat "3/7  2,000 synthetic mandates, six months of history"
run "$GRACE seed --n 2000 --run demo 2>&1 | tail -14"
wait_ 5

beat "4/7  Three arms: do nothing, a rules baseline, the agent"
run "$GRACE run-batch --run demo --offline 2>&1 | grep -vE 'adjudicated' | tail -24"
wait_ 6

beat "5/7  Four decisions, with the reasoning that produced them"
A=$(pick A); B2=$(pick B); CC=$(pick C); D=$(pick D)
if [ -n "$A" ]; then
  say "A) PRE-EMPTIVE. No failure yet. Debit lands before salary; it has bounced before."
  say "   Still ACTIVE, so pause is still legal. This is the whole product."
  run "$GRACE audit $A --run demo | tail -22"; wait_ 8
else say "A) no pre-emptive case in this cohort - reseed"; fi
if [ -n "$B2" ]; then
  say "B) CANCEL INTENT becomes a pause, not a termination."
  run "$GRACE audit $B2 --run demo | tail -20"; wait_ 8
fi
if [ -n "$CC" ]; then
  say "C) The BASELINE does the naive thing - 'it failed, charge it again' - and walks"
  say "   into the eMandate confirmation window. The guard blocks it."
  run "$GRACE audit $CC --run demo --arm baseline | tail -14"; wait_ 8
fi
if [ -n "$D" ]; then
  say "D) Thin evidence: escalate rather than guess. Escalation is cheap."
  run "$GRACE audit $D --run demo | tail -14"; wait_ 6
fi

beat "6/7  The counterfactual: same batch, guard off"
say "Blocked charges become REAL double debits. Grace never auto-refunds one."
run "$GRACE run-batch --run demo --offline --no-guard --arms baseline 2>&1 | grep -E 'guard blocks|double debits'"
say "Restoring the guarded run..."
run "$GRACE run-batch --run demo --offline --arms baseline >/dev/null 2>&1; echo restored"
wait_ 6

beat "7/7  Scored on the holdout, against a baseline I wrote myself"
run "$GRACE eval --run demo 2>&1 | sed -n '2,26p'"
say "Read the false-intervention row before the rupee row."
say "The agent acts more often, and some of that lands on healthy customers."
wait_ 8

beat "What broke"
run "grep -E '^## ' docs/WHAT-BROKE.md | head -14"
printf '\n%sRepo:%s https://github.com/defaultname-ayan/grace-mandates\n' "$B" "$N"
printf '%sReport:%s runs/demo/report.html\n\n' "$B" "$N"
