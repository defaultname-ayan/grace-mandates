"""ClaudeAdjudicator plumbing, verified against a fake SDK.

No network. These assert the request SHAPE and every failure path, which is
what actually breaks: a wrong parameter name, a missing cache breakpoint, or a
retry that silently drops the batch. The model's judgement quality can only be
measured with real credentials -- see README, 'What is not verified'.
"""
from __future__ import annotations

import sys
import types

import pytest

from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Customer, Evidence, Mandate, Rail, SubStatus
from grace.policy import allowed_actions


class FakeUsage:
    input_tokens = 1200
    output_tokens = 180
    cache_read_input_tokens = 1000


class FakeResp:
    def __init__(self, parsed, stop_reason="end_turn"):
        self.parsed_output = parsed
        self.stop_reason = stop_reason
        self.stop_details = None
        self.usage = FakeUsage()
        self._request_id = "req_fake_1"


def install_fake_anthropic(monkeypatch, behaviour):
    """Install a stand-in `anthropic` module whose messages.parse runs `behaviour`."""
    mod = types.ModuleType("anthropic")

    class APIError(Exception):
        pass

    class APIStatusError(APIError):
        def __init__(self, msg="", status_code=500):
            super().__init__(msg)
            self.status_code = status_code

    class RateLimitError(APIStatusError):
        def __init__(self, msg="rate limited"):
            super().__init__(msg, 429)

    class APIConnectionError(APIError):
        pass

    calls: list[dict] = []

    class Messages:
        def parse(self, **kw):
            calls.append(kw)
            return behaviour(len(calls), kw)

    class Anthropic:
        def __init__(self, *a, **k):
            self.messages = Messages()

    mod.Anthropic = Anthropic
    mod.APIError = APIError
    mod.APIStatusError = APIStatusError
    mod.RateLimitError = RateLimitError
    mod.APIConnectionError = APIConnectionError
    monkeypatch.setitem(sys.modules, "anthropic", mod)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return calls


def sample_evidence() -> Evidence:
    m = Mandate(id="simsub_c1", customer_id="c1", rail=Rail.UPI_AUTOPAY,
                plan_amount_paise=49900, cycle_day=5, status=SubStatus.ACTIVE,
                paid_count=4, total_count=12)
    c = Customer(id="c1", bank="HDFC Bank", salary_day=10, tenure_months=14, ltv_band="mid")
    return Evidence(mandate=m, customer=c, bank_health={"td_pct": 0.4},
                    days_to_salary=3, p_fail=0.4,
                    allowed_actions=allowed_actions(Rail.UPI_AUTOPAY, SubStatus.ACTIVE))


GOOD = Decision(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.8, action=Action.PAUSE,
                action_confidence=0.75, pause_cycles=1, rationale="ok")


def test_request_shape_is_correct(monkeypatch):
    calls = install_fake_anthropic(monkeypatch, lambda n, kw: FakeResp(GOOD))
    from grace.adjudicate.claude import ClaudeAdjudicator

    adj = ClaudeAdjudicator(model="claude-opus-5", effort="high")
    d, meta = adj.adjudicate(sample_evidence())

    kw = calls[0]
    assert kw["model"] == "claude-opus-5"
    assert kw["thinking"] == {"type": "adaptive"}, "adaptive thinking, never budget_tokens"
    assert kw["output_config"] == {"effort": "high"}, "effort lives inside output_config"
    assert kw["output_format"] is Decision, "structured output via messages.parse"
    assert "budget_tokens" not in str(kw), "budget_tokens is rejected on Opus 5"
    sys_block = kw["system"][0]
    assert sys_block["cache_control"] == {"type": "ephemeral"}, "system prompt must be cached"
    assert kw["messages"][0]["role"] == "user"
    assert d.action == Action.PAUSE
    assert meta["input_tokens"] == 1200 and meta["cache_read_input_tokens"] == 1000
    assert meta["request_id"] == "req_fake_1"


def test_metas_accumulate_for_cost_reporting(monkeypatch):
    install_fake_anthropic(monkeypatch, lambda n, kw: FakeResp(GOOD))
    from grace.adjudicate.claude import ClaudeAdjudicator

    adj = ClaudeAdjudicator()
    for _ in range(3):
        adj.decide(sample_evidence())
    assert len(adj.metas) == 3


def test_rate_limit_is_retried_then_succeeds(monkeypatch):
    import sys as _s

    def behaviour(n, kw):
        if n < 3:
            raise _s.modules["anthropic"].RateLimitError()
        return FakeResp(GOOD)

    calls = install_fake_anthropic(monkeypatch, behaviour)
    from grace.adjudicate.claude import ClaudeAdjudicator

    d, meta = ClaudeAdjudicator().adjudicate(sample_evidence())
    assert d.action == Action.PAUSE and len(calls) == 3 and meta["attempt"] == 2


def test_server_error_is_retried_but_client_error_is_not(monkeypatch):
    import sys as _s
    from grace.adjudicate.claude import AdjudicationError

    calls = install_fake_anthropic(
        monkeypatch, lambda n, kw: (_ for _ in ()).throw(
            _s.modules["anthropic"].APIStatusError("bad request", 400)))
    from grace.adjudicate.claude import ClaudeAdjudicator

    with pytest.raises(AdjudicationError):
        ClaudeAdjudicator().adjudicate(sample_evidence())
    assert len(calls) == 1, "a 400 must not be retried"


def test_exhausted_retries_raise_not_hang(monkeypatch):
    import sys as _s
    from grace.adjudicate.claude import AdjudicationError

    install_fake_anthropic(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        _s.modules["anthropic"].APIConnectionError("down")))
    from grace.adjudicate.claude import ClaudeAdjudicator

    with pytest.raises(AdjudicationError):
        ClaudeAdjudicator(max_retries=1).adjudicate(sample_evidence())


def test_refusal_is_surfaced_not_silently_accepted(monkeypatch):
    from grace.adjudicate.claude import AdjudicationError

    install_fake_anthropic(monkeypatch, lambda n, kw: FakeResp(GOOD, stop_reason="refusal"))
    from grace.adjudicate.claude import ClaudeAdjudicator

    with pytest.raises(AdjudicationError):
        ClaudeAdjudicator(max_retries=0).adjudicate(sample_evidence())


def test_unparsable_output_is_an_error(monkeypatch):
    from grace.adjudicate.claude import AdjudicationError

    install_fake_anthropic(monkeypatch, lambda n, kw: FakeResp(None))
    from grace.adjudicate.claude import ClaudeAdjudicator

    with pytest.raises(AdjudicationError):
        ClaudeAdjudicator(max_retries=0).adjudicate(sample_evidence())


def test_model_output_is_clamped_before_it_reaches_policy(monkeypatch):
    wild = Decision(cause=Cause.LIQUIDITY_TIMING, cause_confidence=9.0, action=Action.PAUSE,
                    action_confidence=-3.0, pause_cycles=99, rationale="x" * 5000)
    install_fake_anthropic(monkeypatch, lambda n, kw: FakeResp(wild))
    from grace.adjudicate.claude import ClaudeAdjudicator

    d, _ = ClaudeAdjudicator().adjudicate(sample_evidence())
    assert d.cause_confidence == 1.0 and d.action_confidence == 0.0
    assert d.pause_cycles == 2 and len(d.rationale) <= 600


def test_batch_survives_a_dead_adjudicator(monkeypatch, tmp_path):
    """One mandate's failure must never kill the batch (spec 8.4)."""
    import sys as _s

    install_fake_anthropic(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        _s.modules["anthropic"].APIConnectionError("down")))
    from grace.adjudicate.claude import ClaudeAdjudicator, safe_default

    adj = ClaudeAdjudicator(max_retries=0)
    try:
        adj.decide(sample_evidence())
        raised = False
    except Exception:
        raised = True
    assert raised, "the adjudicator itself raises"
    d = safe_default("simulated outage")
    assert d.action == Action.ESCALATE, "the orchestrator's fallback never acts"
