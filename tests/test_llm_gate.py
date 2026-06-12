from analytics.llm_gate import (
    LLMGateDecision,
    apply_llm_gate_decision,
    cache_key_for_gate,
    parse_gate_response,
)
from analytics.polymarket_backtest import GammaMarket, NewsEvent
from datetime import datetime, timezone


def _news() -> NewsEvent:
    return NewsEvent(
        id=1,
        ts=datetime(2026, 5, 7, 12, 0, tzinfo=timezone.utc),
        headline="IRAN engage diplomatic cooperation IRAQ in Baghdad, Baghdad, Iraq",
        summary="GDELT event 1; source=example.com; url=https://example.com/story",
        symbols=[],
    )


def _market() -> GammaMarket:
    return GammaMarket(
        id="m1",
        question="Will no qualifying diplomatic US-Iran meeting occur by June 30, 2026?",
        description="Resolves No if a qualifying diplomatic US-Iran meeting occurs.",
        outcomes=["Yes", "No"],
        outcome_prices=[0.65, 0.35],
        clob_token_ids=["yes-token", "no-token"],
        end_date=datetime(2026, 6, 30, tzinfo=timezone.utc),
        closed_time=None,
        enable_order_book=True,
        volume=1000.0,
        liquidity=100.0,
        closed=False,
        active=True,
        event_title="US-Iran diplomatic meeting",
    )


def test_parse_gate_response_accepts_json_embedded_in_text():
    text = 'Result: {"valid_match": true, "p_yes": 0.22, "confidence": 0.81, "reason": "event suggests meeting"}'

    decision = parse_gate_response(text)

    assert decision.valid_match is True
    assert decision.p_yes == 0.22
    assert decision.confidence == 0.81
    assert decision.reason == "event suggests meeting"


def test_parse_gate_response_rejects_missing_required_fields():
    decision = parse_gate_response('{"valid_match": true, "reason": "missing probability"}')

    assert decision.valid_match is False
    assert decision.p_yes == 0.5
    assert "parse error" in decision.reason


def test_cache_key_for_gate_is_stable_and_market_specific():
    key1 = cache_key_for_gate(_news(), _market())
    key2 = cache_key_for_gate(_news(), _market())
    other_market = GammaMarket(**{**_market().__dict__, "id": "m2"})

    assert key1 == key2
    assert key1 != cache_key_for_gate(_news(), other_market)


def test_apply_llm_gate_decision_rejects_invalid_match():
    decision = LLMGateDecision(valid_match=False, p_yes=0.5, confidence=0.9, reason="not same event")

    assert apply_llm_gate_decision(decision) is None


def test_apply_llm_gate_decision_uses_probability_for_side():
    yes = LLMGateDecision(valid_match=True, p_yes=0.76, confidence=0.8, reason="supports yes")
    no = LLMGateDecision(valid_match=True, p_yes=0.24, confidence=0.8, reason="supports no")

    assert apply_llm_gate_decision(yes) == ("yes", 0.76, "supports yes")
    assert apply_llm_gate_decision(no) == ("no", 0.76, "supports no")
