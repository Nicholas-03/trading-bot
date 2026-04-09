import pytest
from llm_advisor import Decision, _parse_response


def test_parse_buy_decision():
    text = '{"action": "buy", "ticker": "AAPL", "reasoning": "Strong earnings beat"}'
    decision = _parse_response(text)
    assert decision.action == "buy"
    assert decision.ticker == "AAPL"
    assert "earnings" in decision.reasoning


def test_parse_sell_decision():
    text = '{"action": "sell", "ticker": "TSLA", "reasoning": "Product recall announced"}'
    decision = _parse_response(text)
    assert decision.action == "sell"
    assert decision.ticker == "TSLA"


def test_parse_hold_decision():
    text = '{"action": "hold", "ticker": null, "reasoning": "Neutral news"}'
    decision = _parse_response(text)
    assert decision.action == "hold"
    assert decision.ticker is None


def test_parse_response_with_surrounding_text():
    text = 'Sure! Here is my answer:\n{"action": "buy", "ticker": "NVDA", "reasoning": "AI demand"}\nHope that helps.'
    decision = _parse_response(text)
    assert decision.action == "buy"
    assert decision.ticker == "NVDA"


def test_parse_invalid_response_raises():
    with pytest.raises(ValueError):
        _parse_response("I cannot determine what to do here.")


def test_parse_invalid_action_raises():
    text = '{"action": "Buy", "ticker": "AAPL", "reasoning": "good news"}'
    with pytest.raises(ValueError, match="Unexpected action"):
        _parse_response(text)


def test_parse_hold_with_string_null_ticker():
    text = '{"action": "hold", "ticker": "null", "reasoning": "Neutral"}'
    decision = _parse_response(text)
    assert decision.ticker is None


def test_parse_picks_first_valid_json_when_multiple_present():
    text = 'Context: {"not": "valid decision"} Final: {"action": "buy", "ticker": "MSFT", "reasoning": "Strong results"}'
    decision = _parse_response(text)
    assert decision.action == "buy"
    assert decision.ticker == "MSFT"


def test_parse_short_decision():
    text = '{"action": "short", "ticker": "META", "reasoning": "Revenue miss and weak guidance"}'
    decision = _parse_response(text)
    assert decision.action == "short"
    assert decision.ticker == "META"
