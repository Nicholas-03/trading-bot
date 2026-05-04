import asyncio
from unittest.mock import AsyncMock
import pytest
from llm.multi_advisor import MultiDecision, MultiLLMAdvisor, ProviderResult
from llm.llm_advisor import Decision
from llm.providers.base import CompletionResult


def _make_completion(text: str) -> CompletionResult:
    return CompletionResult(text=text, input_tokens=100, output_tokens=50)


def _make_advisor(
    claude_response: str,
    gemini_response: str,
    deepseek_response: str,
    chatgpt_response: str,
) -> MultiLLMAdvisor:
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = _make_completion(claude_response)
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _make_completion(gemini_response)
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _make_completion(deepseek_response)
    chatgpt_mock = AsyncMock()
    chatgpt_mock.complete.return_value = _make_completion(chatgpt_response)
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    advisor._chatgpt = chatgpt_mock
    advisor._claude_model = "claude-haiku-4-5"
    advisor._gemini_model = "gemini-2.5-flash"
    advisor._deepseek_model = "deepseek-v4-flash"
    advisor._chatgpt_model = "gpt-5.4-mini"
    return advisor


_BUY_JSON = '{"action":"buy","ticker":"AAPL","reasoning":"strong earnings","confidence":0.9,"hold_hours":2}'
_HOLD_JSON = '{"action":"hold","ticker":null,"reasoning":"unsure","confidence":0.0,"hold_hours":0}'


def test_primary_falls_back_to_claude_when_no_majority():
    # Only Claude says buy — no majority → primary is Claude's decision
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert isinstance(result, MultiDecision)
    assert result.primary.action == "buy"
    assert result.primary.ticker == "AAPL"
    assert result.primary_provider == "claude"


_BUY_HIGH_JSON = '{"action":"buy","ticker":"AAPL","reasoning":"strong","confidence":0.95,"hold_hours":2}'
_BUY_LOW_JSON = '{"action":"buy","ticker":"AAPL","reasoning":"ok","confidence":0.75,"hold_hours":2}'


def test_primary_uses_majority_vote_picks_highest_confidence():
    # 3 providers say buy AAPL; highest confidence (0.95) should be primary
    advisor = _make_advisor(_BUY_LOW_JSON, _BUY_HIGH_JSON, _BUY_LOW_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert result.primary.action == "buy"
    assert result.primary.ticker == "AAPL"
    assert result.primary.confidence == 0.95
    assert result.primary_provider == "gemini"


def test_primary_uses_majority_vote_two_out_of_four():
    # Exactly 2 providers agree: gemini + deepseek both say buy
    _BUY_GEMINI = '{"action":"buy","ticker":"TSLA","reasoning":"catalyst","confidence":0.85,"hold_hours":4}'
    _BUY_DEEPSEEK = '{"action":"buy","ticker":"TSLA","reasoning":"catalyst","confidence":0.80,"hold_hours":4}'
    advisor = _make_advisor(_HOLD_JSON, _BUY_GEMINI, _BUY_DEEPSEEK, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["TSLA"], set(), set(), 0.0))
    assert result.primary.action == "buy"
    assert result.primary.ticker == "TSLA"
    assert result.primary.confidence == 0.85
    assert result.primary_provider == "gemini"


def test_all_results_ordered_claude_gemini_deepseek_chatgpt():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    providers = [r.provider for r in result.all_results]
    assert providers == ["claude", "gemini", "deepseek", "chatgpt"]


def test_claude_error_falls_back_to_hold():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.side_effect = RuntimeError("API down")
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _make_completion(_HOLD_JSON)
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _make_completion(_HOLD_JSON)
    chatgpt_mock = AsyncMock()
    chatgpt_mock.complete.return_value = _make_completion(_HOLD_JSON)
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    advisor._chatgpt = chatgpt_mock
    advisor._claude_model = "claude-haiku-4-5"
    advisor._gemini_model = "gemini-2.5-flash"
    advisor._deepseek_model = "deepseek-v4-flash"
    advisor._chatgpt_model = "gpt-5.4-mini"

    result = asyncio.run(advisor.analyze("headline", "summary", [], set(), set(), 0.0))
    assert result.primary.action == "hold"
    assert "error" in result.primary.reasoning.lower()


def test_latency_sec_always_non_negative():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    for pr in result.all_results:
        assert isinstance(pr, ProviderResult)
        assert pr.latency_sec >= 0.0


def test_partial_provider_error_still_returns_four_results():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = _make_completion(_BUY_JSON)
    gemini_mock = AsyncMock()
    gemini_mock.complete.side_effect = RuntimeError("timeout")
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _make_completion(_HOLD_JSON)
    chatgpt_mock = AsyncMock()
    chatgpt_mock.complete.return_value = _make_completion(_HOLD_JSON)
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    advisor._chatgpt = chatgpt_mock
    advisor._claude_model = "claude-haiku-4-5"
    advisor._gemini_model = "gemini-2.5-flash"
    advisor._deepseek_model = "deepseek-v4-flash"
    advisor._chatgpt_model = "gpt-5.4-mini"

    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert len(result.all_results) == 4
    assert result.all_results[1].provider == "gemini"
    assert result.all_results[1].decision.action == "hold"
    assert "error" in result.all_results[1].decision.reasoning.lower()


def test_chatgpt_error_does_not_affect_primary():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = _make_completion(_BUY_JSON)
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _make_completion(_HOLD_JSON)
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _make_completion(_HOLD_JSON)
    chatgpt_mock = AsyncMock()
    chatgpt_mock.complete.side_effect = RuntimeError("rate limited")
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    advisor._chatgpt = chatgpt_mock
    advisor._claude_model = "claude-haiku-4-5"
    advisor._gemini_model = "gemini-2.5-flash"
    advisor._deepseek_model = "deepseek-v4-flash"
    advisor._chatgpt_model = "gpt-5.4-mini"

    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert result.primary.action == "buy"
    assert result.all_results[3].provider == "chatgpt"
    assert result.all_results[3].decision.action == "hold"
    assert "error" in result.all_results[3].decision.reasoning.lower()


def test_cost_usd_populated_for_known_models():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    for pr in result.all_results:
        assert pr.cost_usd is not None
        assert pr.cost_usd >= 0.0


def test_cost_usd_is_none_for_unknown_model():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON, _HOLD_JSON)
    advisor._claude_model = "unknown-model-xyz"
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert result.all_results[0].cost_usd is None


def test_cost_usd_is_none_on_provider_error():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.side_effect = RuntimeError("API down")
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _make_completion(_HOLD_JSON)
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _make_completion(_HOLD_JSON)
    chatgpt_mock = AsyncMock()
    chatgpt_mock.complete.return_value = _make_completion(_HOLD_JSON)
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    advisor._chatgpt = chatgpt_mock
    advisor._claude_model = "claude-haiku-4-5"
    advisor._gemini_model = "gemini-2.5-flash"
    advisor._deepseek_model = "deepseek-v4-flash"
    advisor._chatgpt_model = "gpt-5.4-mini"

    result = asyncio.run(advisor.analyze("headline", "summary", [], set(), set(), 0.0))
    assert result.all_results[0].cost_usd is None
