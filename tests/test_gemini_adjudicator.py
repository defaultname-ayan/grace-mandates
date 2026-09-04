"""GeminiAdjudicator plumbing, verified against a fake SDK. No network.

Gemini's failure modes differ from Anthropic's in a way that matters: a refusal
arrives as a `finish_reason` on a candidate (or a `prompt_feedback.block_reason`)
with an otherwise well-formed empty response. Miss that check and a safety
decision looks like a malformed reply and gets retried.
"""
from __future__ import annotations

import sys
import types as pytypes

import pytest

from grace.adjudicate.schema import Decision
from grace.models import Action, Cause, Customer, Evidence, Mandate, Rail, SubStatus
from grace.policy import allowed_actions

GOOD = Decision(cause=Cause.LIQUIDITY_TIMING, cause_confidence=0.8, action=Action.PAUSE,
                action_confidence=0.75, pause_cycles=1, rationale="ok")


class FakeUsage:
    prompt_token_count = 1500
    candidates_token_count = 210
    thoughts_token_count = 340
    cached_content_token_count = 1200


class FakeCandidate:
    def __init__(self, finish_reason="STOP"):
        self.finish_reason = finish_reason


class FakePromptFeedback:
    def __init__(self, block_reason=None):
        self.block_reason = block_reason


class FakeResp:
    def __init__(self, parsed=GOOD, finish_reason="STOP", block_reason=None):
        self.parsed = parsed
        self.candidates = [FakeCandidate(finish_reason)]
        self.prompt_feedback = FakePromptFeedback(block_reason)
        self.usage_metadata = FakeUsage()
        self.response_id = "resp_fake_1"


def install_fake_genai(monkeypatch, behaviour):
    """Stand in for google.genai, preserving the real types module."""
    from google.genai import types as real_types

    calls: list[dict] = []

    class Models:
        def generate_content(self, **kw):
            calls.append(kw)
            return behaviour(len(calls), kw)

    class Client:
        def __init__(self, *a, **k):
            self.models = Models()

    genai_mod = pytypes.ModuleType("google.genai")
    genai_mod.Client = Client
    genai_mod.types = real_types

    class APIError(Exception):
        def __init__(self, message="", code=None):
            super().__init__(message)
            self.code = code
            self.message = message

    class ClientError(APIError):
        pass

    class ServerError(APIError):
        pass

    errors_mod = pytypes.ModuleType("google.genai.errors")
    errors_mod.APIError = APIError
    errors_mod.ClientError = ClientError
    errors_mod.ServerError = ServerError
    genai_mod.errors = errors_mod

    google_pkg = pytypes.ModuleType("google")
    google_pkg.genai = genai_mod

    monkeypatch.setitem(sys.modules, "google", google_pkg)
    monkeypatch.setitem(sys.modules, "google.genai", genai_mod)
    monkeypatch.setitem(sys.modules, "google.genai.errors", errors_mod)
    monkeypatch.setitem(sys.modules, "google.genai.types", real_types)
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return calls, errors_mod


def sample_evidence() -> Evidence:
    m = Mandate(id="simsub_g1", customer_id="c1", rail=Rail.UPI_AUTOPAY,
                plan_amount_paise=49900, cycle_day=5, status=SubStatus.ACTIVE,
                paid_count=4, total_count=12)
    c = Customer(id="c1", bank="HDFC Bank", salary_day=10, tenure_months=14, ltv_band="mid")
    return Evidence(mandate=m, customer=c, bank_health={"td_pct": 0.4},
                    days_to_salary=3, p_fail=0.4,
                    allowed_actions=allowed_actions(Rail.UPI_AUTOPAY, SubStatus.ACTIVE))


def make(monkeypatch, behaviour, **kw):
    calls, errs = install_fake_genai(monkeypatch, behaviour)
    from grace.adjudicate.gemini import GeminiAdjudicator

    return GeminiAdjudicator(**kw), calls, errs


# ------------------------------------------------------------- request shape
def test_request_shape_matches_the_gemini_3_contract(monkeypatch):
    adj, calls, _ = make(monkeypatch, lambda n, kw: FakeResp(), model="gemini-3.8-flash",
                         effort="high")
    d, meta = adj.adjudicate(sample_evidence())

    kw = calls[0]
    cfg = kw["config"]
    assert kw["model"] == "gemini-3.8-flash"
    assert cfg.response_mime_type == "application/json"
    assert cfg.response_schema is Decision
    assert cfg.system_instruction, "the static prompt goes in system_instruction"
    assert str(cfg.thinking_config.thinking_level).upper().endswith("HIGH")
    assert cfg.thinking_config.thinking_budget is None, \
        "sending thinking_level AND thinking_budget together is a 400 on Gemini 3"
    assert cfg.temperature is None, \
        "Gemini 3 guidance: leave temperature at its default 1.0"
    assert d.action == Action.PAUSE


def test_effort_maps_onto_thinking_level(monkeypatch):
    for effort, want in [("low", "LOW"), ("medium", "MEDIUM"), ("high", "HIGH"),
                         ("max", "HIGH"), ("xhigh", "HIGH")]:
        adj, calls, _ = make(monkeypatch, lambda n, kw: FakeResp(), effort=effort)
        adj.adjudicate(sample_evidence())
        assert str(calls[0]["config"].thinking_config.thinking_level).upper().endswith(want)


def test_usage_is_reported_including_thinking_tokens(monkeypatch):
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp())
    _, meta = adj.adjudicate(sample_evidence())
    assert meta["input_tokens"] == 1500
    assert meta["output_tokens"] == 210
    assert meta["thinking_tokens"] == 340, "thinking tokens are billed and must be visible"
    assert meta["cache_read_input_tokens"] == 1200
    assert meta["request_id"] == "resp_fake_1"


def test_metas_accumulate_for_cost_reporting(monkeypatch):
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp())
    for _ in range(3):
        adj.decide(sample_evidence())
    assert len(adj.metas) == 3


# ------------------------------------------------------------------ refusals
@pytest.mark.parametrize("reason", ["SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"])
def test_finish_reason_refusals_are_surfaced(monkeypatch, reason):
    from grace.adjudicate.gemini import GeminiRefusal

    adj, calls, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=None, finish_reason=reason))
    with pytest.raises(GeminiRefusal):
        adj.adjudicate(sample_evidence())
    assert len(calls) == 1, "a refusal is a decision, not a fault: it must not be retried"


def test_blocked_prompt_is_surfaced(monkeypatch):
    from grace.adjudicate.gemini import GeminiRefusal

    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=None, block_reason="SAFETY"))
    with pytest.raises(GeminiRefusal):
        adj.adjudicate(sample_evidence())


def test_unspecified_block_reason_is_not_a_refusal(monkeypatch):
    adj, _, _ = make(monkeypatch,
                     lambda n, kw: FakeResp(block_reason="BLOCKED_REASON_UNSPECIFIED"))
    d, _ = adj.adjudicate(sample_evidence())
    assert d.action == Action.PAUSE


def test_truncated_response_is_an_error_not_a_silent_noop(monkeypatch):
    """MAX_TOKENS means the JSON is cut off; parsed would be None."""
    from grace.adjudicate.gemini import AdjudicationError

    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=None, finish_reason="MAX_TOKENS"),
                     max_retries=0)
    with pytest.raises(AdjudicationError, match="truncated"):
        adj.adjudicate(sample_evidence())


def test_unparsable_output_is_an_error(monkeypatch):
    from grace.adjudicate.gemini import AdjudicationError

    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=None), max_retries=0)
    with pytest.raises(AdjudicationError):
        adj.adjudicate(sample_evidence())


def test_dict_parsed_output_is_coerced(monkeypatch):
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=GOOD.model_dump()))
    d, _ = adj.adjudicate(sample_evidence())
    assert isinstance(d, Decision) and d.action == Action.PAUSE


# -------------------------------------------------------------------- retries
def test_server_error_is_retried_then_succeeds(monkeypatch):
    def behaviour(n, kw):
        if n < 3:
            raise sys.modules["google.genai.errors"].ServerError("503 unavailable", 503)
        return FakeResp()

    adj, calls, _ = make(monkeypatch, behaviour)
    d, meta = adj.adjudicate(sample_evidence())
    assert d.action == Action.PAUSE and len(calls) == 3 and meta["attempt"] == 2


def test_rate_limit_moves_to_the_next_model_without_sleeping(monkeypatch):
    """A daily quota does not recover in seconds; when another model exists,
    a 429 must fall through immediately and put the model into cooldown."""
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", lambda s: slept.append(s))

    def behaviour(n, kw):
        if kw["model"] == "gemini-3.8-flash":
            raise sys.modules["google.genai.errors"].ClientError("429 quota", 429)
        return FakeResp()

    adj, calls, _ = make(monkeypatch, behaviour)
    d, meta = adj.adjudicate(sample_evidence())
    assert len(calls) == 2 and meta["model"] == "gemini-3.7-flash"
    assert slept == [], "no backoff on a quota error when a fallback exists"
    assert adj._cooldown_until.get("gemini-3.8-flash", 0) > 0
    adj.adjudicate(sample_evidence())
    assert calls[-1]["model"] == "gemini-3.7-flash", "cooled-down model must be skipped"


def test_rate_limit_on_the_last_model_is_retried(monkeypatch):
    def behaviour(n, kw):
        if n < 2:
            raise sys.modules["google.genai.errors"].ClientError("429 quota", 429)
        return FakeResp()

    adj, calls, _ = make(monkeypatch, behaviour, model="gemini-3.5-flash-lite", pin=True)
    adj.adjudicate(sample_evidence())
    assert len(calls) == 2 and len(adj.model_chain) == 1


def test_client_error_other_than_429_is_not_retried_within_a_model(monkeypatch):
    """A 400 is not transient, so it must not be retried against the same model.

    It IS worth trying the next model: 'Thinking level is not supported' is a
    real, model-specific 400 that the fallback chain should survive.
    """
    from grace.adjudicate.gemini import AdjudicationError

    adj, calls, _ = make(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        sys.modules["google.genai.errors"].ClientError("400 bad request", 400)))
    with pytest.raises(AdjudicationError):
        adj.adjudicate(sample_evidence())
    assert len(calls) == len(adj.model_chain), "one attempt per model, no retries within one"
    assert len({c["model"] for c in calls}) == len(adj.model_chain), "each model tried once"


def test_exhausted_retries_raise(monkeypatch):
    from grace.adjudicate.gemini import AdjudicationError

    adj, _, _ = make(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        sys.modules["google.genai.errors"].ServerError("500", 500)), max_retries=1)
    with pytest.raises(AdjudicationError):
        adj.adjudicate(sample_evidence())


def test_wild_output_is_clamped_before_policy_sees_it(monkeypatch):
    wild = Decision(cause=Cause.LIQUIDITY_TIMING, cause_confidence=9.0, action=Action.PAUSE,
                    action_confidence=-3.0, pause_cycles=99, rationale="x" * 5000)
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp(parsed=wild))
    d, _ = adj.adjudicate(sample_evidence())
    assert d.cause_confidence == 1.0 and d.action_confidence == 0.0
    assert d.pause_cycles == 2 and len(d.rationale) <= 600


def test_missing_key_fails_with_a_useful_message(monkeypatch):
    install_fake_genai(monkeypatch, lambda n, kw: FakeResp())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    from grace.adjudicate.gemini import GeminiAdjudicator

    with pytest.raises(RuntimeError, match="aistudio.google.com"):
        GeminiAdjudicator()


# ------------------------------------------------------------- provider swap
def test_both_providers_satisfy_the_same_interface(monkeypatch):
    install_fake_genai(monkeypatch, lambda n, kw: FakeResp())
    from grace.adjudicate import make_llm_adjudicator

    g = make_llm_adjudicator("gemini")
    assert hasattr(g, "decide") and hasattr(g, "metas") and g.name == "gemini"


def test_unknown_provider_is_rejected():
    from grace.adjudicate import make_llm_adjudicator

    with pytest.raises(ValueError, match="unknown GRACE_PROVIDER"):
        make_llm_adjudicator("openai")


# ------------------------------------------------- model-family compatibility
@pytest.mark.parametrize("model,expected", [
    ("gemini-3.8-flash", True), ("gemini-3.5-flash", True), ("gemini-3.1-flash-lite", True),
    ("models/gemini-3.7-flash", True),
    ("gemini-2.5-flash", False), ("gemini-2.5-pro", False), ("gemini-2.0-flash", False),
    ("gemini-flash-latest", False), ("", False),
])
def test_thinking_level_only_sent_to_models_that_accept_it(model, expected):
    """gemini-2.5-flash returns 400 'Thinking level is not supported'.
    Verified live before this guard existed."""
    from grace.adjudicate.gemini import supports_thinking_level

    assert supports_thinking_level(model) is expected


def test_config_omits_thinking_level_on_gemini_2x(monkeypatch):
    adj, calls, _ = make(monkeypatch, lambda n, kw: FakeResp(), model="gemini-2.5-flash")
    adj.adjudicate(sample_evidence())
    cfg = calls[0]["config"]
    assert cfg.thinking_config is None, "sending thinking_level to a 2.x model is a 400"
    assert cfg.response_schema is Decision, "structured output still applies"


def test_config_includes_thinking_level_on_gemini_3x(monkeypatch):
    adj, calls, _ = make(monkeypatch, lambda n, kw: FakeResp(), model="gemini-3.8-flash")
    adj.adjudicate(sample_evidence())
    assert calls[0]["config"].thinking_config is not None


def test_backoff_grows_and_is_jittered(monkeypatch):
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp())
    assert adj._backoff(0) < adj._backoff(4) <= 30.0 * 1.4
    assert len({round(adj._backoff(2), 6) for _ in range(20)}) > 1, "must be jittered"


# ------------------------------------------------------------ fallback chain
def test_falls_back_to_the_next_model_when_one_is_overloaded(monkeypatch):
    """503 'high demand' on the free tier is not fixable by backoff."""
    def behaviour(n, kw):
        if kw["model"] == "gemini-3.8-flash":
            raise sys.modules["google.genai.errors"].ServerError("503 high demand", 503)
        return FakeResp()

    adj, calls, _ = make(monkeypatch, behaviour, max_retries=1)
    d, meta = adj.adjudicate(sample_evidence())
    assert d.action == Action.PAUSE
    assert meta["model"] == "gemini-3.7-flash", "served by the next model down"
    assert meta["requested_model"] == "gemini-3.8-flash"
    assert meta["fallback_depth"] == 1, "the report must be able to see this happened"


def test_refusal_is_never_shopped_to_another_model(monkeypatch):
    """A safety decision is a decision. Retrying it elsewhere would be
    laundering a refusal, not recovering from a fault."""
    from grace.adjudicate.gemini import GeminiRefusal

    adj, calls, _ = make(monkeypatch,
                         lambda n, kw: FakeResp(parsed=None, finish_reason="SAFETY"))
    with pytest.raises(GeminiRefusal):
        adj.adjudicate(sample_evidence())
    assert len(calls) == 1, "must not try the next model after a refusal"


def test_all_models_failing_raises_clearly(monkeypatch):
    from grace.adjudicate.gemini import AdjudicationError

    adj, _, _ = make(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        sys.modules["google.genai.errors"].ServerError("503", 503)), max_retries=0)
    with pytest.raises(AdjudicationError, match=r"all \d+ models"):
        adj.adjudicate(sample_evidence())


def test_successful_first_model_records_zero_fallback_depth(monkeypatch):
    adj, _, _ = make(monkeypatch, lambda n, kw: FakeResp())
    _, meta = adj.adjudicate(sample_evidence())
    assert meta["fallback_depth"] == 0 and meta["model"] == meta["requested_model"]


def test_pinned_model_has_no_fallback_chain(monkeypatch):
    """`--model X` must mean X: a failing pinned model reported as a failure,
    never quietly served by the default chain."""
    from grace.adjudicate import make_llm_adjudicator
    from grace.adjudicate.gemini import AdjudicationError

    install_fake_genai(monkeypatch, lambda n, kw: (_ for _ in ()).throw(
        sys.modules["google.genai.errors"].ServerError("503", 503)))
    adj = make_llm_adjudicator("gemini", model="gemini-2.5-flash")
    assert adj.model_chain == ["gemini-2.5-flash"]
    with pytest.raises(AdjudicationError):
        adj.adjudicate(sample_evidence())


def test_both_providers_share_one_error_type():
    from grace.adjudicate import claude, gemini
    from grace.adjudicate.base import AdjudicationError

    assert claude.AdjudicationError is AdjudicationError
    assert gemini.AdjudicationError is AdjudicationError
    assert issubclass(gemini.GeminiRefusal, AdjudicationError)
