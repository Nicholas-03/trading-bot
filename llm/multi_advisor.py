import asyncio
import logging
import time
from dataclasses import dataclass

from llm.llm_advisor import Decision, _PROMPT_TEMPLATE, _parse_response
from llm.providers import ChatGPTProvider, ClaudeProvider, DeepSeekProvider, GeminiProvider

logger = logging.getLogger(__name__)


@dataclass
class ProviderResult:
    provider: str
    decision: Decision
    latency_sec: float


@dataclass
class MultiDecision:
    primary: Decision
    all_results: list[ProviderResult]


class MultiLLMAdvisor:
    def __init__(self, config) -> None:
        self._claude = ClaudeProvider(config.anthropic_api_key, config.anthropic_model)
        self._gemini = GeminiProvider(config.google_api_key, config.gemini_model)
        self._deepseek = DeepSeekProvider(config.deepseek_api_key, config.deepseek_model)
        self._chatgpt = ChatGPTProvider(config.openai_api_key, config.openai_model)

    async def _call(self, provider_name: str, provider, prompt: str) -> ProviderResult:
        start = time.monotonic()
        try:
            text = await provider.complete(prompt)
            decision = _parse_response(text)
        except Exception as exc:
            logger.warning("Provider %s error: %s", provider_name, exc)
            decision = Decision(action="hold", ticker=None, reasoning=f"error: {exc}")
        return ProviderResult(
            provider=provider_name,
            decision=decision,
            latency_sec=time.monotonic() - start,
        )

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
        news_age_hours: float = 0.0,
    ) -> MultiDecision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
            shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
            news_age_hours=news_age_hours,
        )
        results: list[ProviderResult] = await asyncio.gather(
            self._call("claude", self._claude, prompt),
            self._call("gemini", self._gemini, prompt),
            self._call("deepseek", self._deepseek, prompt),
            self._call("chatgpt", self._chatgpt, prompt),
        )
        return MultiDecision(primary=results[0].decision, all_results=results)
