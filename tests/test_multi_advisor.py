import asyncio
from unittest.mock import AsyncMock
import pytest
from llm.multi_advisor import MultiDecision, MultiLLMAdvisor, ProviderResult
from llm.llm_advisor import Decision


def _make_advisor(claude_response: str, gemini_response: str, deepseek_response: str) -> MultiLLMAdvisor:
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = claude_response
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = gemini_response
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = deepseek_response
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    return advisor


_BUY_JSON = '{"action":"buy","ticker":"AAPL","reasoning":"strong earnings","confidence":0.9,"hold_hours":2}'
_HOLD_JSON = '{"action":"hold","ticker":null,"reasoning":"unsure","confidence":0.0,"hold_hours":0}'


def test_primary_is_always_claude_decision():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert isinstance(result, MultiDecision)
    assert result.primary.action == "buy"
    assert result.primary.ticker == "AAPL"


def test_all_results_ordered_claude_gemini_deepseek():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    providers = [r.provider for r in result.all_results]
    assert providers == ["claude", "gemini", "deepseek"]


def test_claude_error_falls_back_to_hold():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.side_effect = RuntimeError("API down")
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _HOLD_JSON
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _HOLD_JSON
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock

    result = asyncio.run(advisor.analyze("headline", "summary", [], set(), set(), 0.0))
    assert result.primary.action == "hold"
    assert "error" in result.primary.reasoning.lower()


def test_latency_sec_always_non_negative():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    for pr in result.all_results:
        assert isinstance(pr, ProviderResult)
        assert pr.latency_sec >= 0.0


def test_partial_provider_error_still_returns_three_results():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = _BUY_JSON
    gemini_mock = AsyncMock()
    gemini_mock.complete.side_effect = RuntimeError("timeout")
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _HOLD_JSON
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock

    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert len(result.all_results) == 3
    assert result.all_results[1].provider == "gemini"
    assert result.all_results[1].decision.action == "hold"
    assert "error" in result.all_results[1].decision.reasoning.lower()
