import json
import logging
from dataclasses import dataclass
from typing import Literal

from config import Config
from llm.providers import ClaudeProvider, DeepSeekProvider, GeminiProvider
from llm.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide the best action.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}

Currently held long positions: {held_tickers}
Currently held short positions: {shorted_tickers}

Actions available:
- buy: open a long position (bet the price goes UP). Only for tickers in the news.
- short: open a short position (bet the price goes DOWN). Only for tickers in the news.
- sell: close an open long OR short position. Only for tickers you currently hold.
- hold: do nothing.

Rules:
- Only act on tickers directly mentioned in the news.
- Be conservative — only act on clearly bullish or clearly bearish news.
- Do not open a long and short on the same ticker simultaneously.
- Return ONLY a valid JSON object, nothing else. Use exactly one of these formats:
  {{"action": "buy", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "short", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "sell", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "hold", "ticker": null, "reasoning": "one sentence"}}
"""

_VALID_ACTIONS = frozenset({"buy", "short", "sell", "hold"})


@dataclass
class Decision:
    action: Literal["buy", "short", "sell", "hold"]
    ticker: str | None
    reasoning: str


def _parse_response(text: str) -> Decision:
    decoder = json.JSONDecoder()
    idx = 0
    last_action_error: ValueError | None = None
    while idx < len(text):
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            data, _ = decoder.raw_decode(text, pos)
            action = data.get("action", "")
            if action not in _VALID_ACTIONS:
                last_action_error = ValueError(f"Unexpected action {action!r}; expected one of {_VALID_ACTIONS}")
                idx = pos + 1
                continue
            raw_ticker = data.get("ticker")
            ticker = None if raw_ticker in (None, "null", "") else str(raw_ticker)
            return Decision(
                action=action,
                ticker=ticker,
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            idx = pos + 1
    if last_action_error is not None:
        raise last_action_error
    raise ValueError(f"No valid decision JSON found in response: {text!r}")


class LLMAdvisor:
    def __init__(self, config: Config) -> None:
        provider = config.llm_provider
        if provider == "claude":
            self._provider: LLMProvider = ClaudeProvider(config.anthropic_api_key, config.anthropic_model)
        elif provider == "gemini":
            self._provider = GeminiProvider(config.google_api_key, config.gemini_model)
        else:
            self._provider = DeepSeekProvider(config.deepseek_api_key, config.deepseek_model)

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
    ) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
            shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
        )
        try:
            text = await self._provider.complete(prompt)
            return _parse_response(text)
        except ValueError as e:
            logger.error("LLM parse error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"parse error: {e}")
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"api error: {e}")
