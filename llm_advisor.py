import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

import anthropic
from config import Config

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-6"

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide whether to buy, sell, or hold.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}

Currently held positions: {held_tickers}

Rules:
- Only recommend buying a ticker directly mentioned in the news.
- Only recommend selling if the news is clearly negative for a ticker you currently hold.
- Be conservative — only act on clearly bullish or clearly bearish news.
- Return ONLY a valid JSON object, nothing else. Use exactly one of these formats:
  {{"action": "buy", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "sell", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "hold", "ticker": null, "reasoning": "one sentence"}}
"""

_VALID_ACTIONS = frozenset({"buy", "sell", "hold"})


@dataclass
class Decision:
    action: Literal["buy", "sell", "hold"]
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
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    async def analyze(self, headline: str, summary: str, symbols: list[str], held_tickers: set[str]) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
        )
        try:
            # Run the blocking Anthropic SDK call in a thread so it doesn't freeze the event loop
            message = await asyncio.to_thread(
                self._client.messages.create,
                model=_MODEL,
                max_tokens=512,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text
            return _parse_response(text)
        except ValueError as e:
            logger.error("LLM parse error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"parse error: {e}")
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"api error: {e}")
