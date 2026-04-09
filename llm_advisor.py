import json
import logging
import re
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
- Return ONLY a JSON object, nothing else:
  {{"action": "buy" | "sell" | "hold", "ticker": "SYMBOL or null", "reasoning": "one sentence"}}
"""


@dataclass
class Decision:
    action: Literal["buy", "sell", "hold"]
    ticker: str | None
    reasoning: str


def _parse_response(text: str) -> Decision:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text!r}")
    data = json.loads(match.group())
    return Decision(
        action=data["action"],
        ticker=data.get("ticker") or None,
        reasoning=data.get("reasoning", ""),
    )


class LLMAdvisor:
    def __init__(self, config: Config) -> None:
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def analyze(self, headline: str, summary: str, symbols: list[str], held_tickers: set[str]) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
        )
        try:
            message = self._client.messages.create(
                model=_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text
            return _parse_response(text)
        except Exception as e:
            logger.error("LLM error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"error: {e}")
