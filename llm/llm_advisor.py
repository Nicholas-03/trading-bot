import json
import logging
from dataclasses import dataclass
from typing import Literal

from config import Config
from llm.providers import ChatGPTProvider, ClaudeProvider
from llm.providers.base import LLMProvider

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide the best action.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}
News age: {news_age_hours:.1f} hours since publication

Currently held long positions: {held_tickers}
Currently held short positions: {shorted_tickers}

Actions available:
- buy: open a long position (bet the price goes UP). Only for tickers in the news.
- short: open a short position (bet the price goes DOWN). Only for tickers in the news.
- sell: close an open long OR short position. Only for tickers you currently hold.
- hold: do nothing.

Rules — evaluate each one before deciding:
1. Only act on tickers directly mentioned in the news.
2. Do not open a long and short on the same ticker simultaneously.
3. REQUIRE a specific, quantifiable catalyst: confirmed earnings beat vs consensus (with actual %), FDA approval/rejection, signed acquisition with deal value, regulatory decision. Do NOT act on analyst upgrades, price target changes, or vague positive sentiment.
4. REJECT retrospective move-explanation articles. Headlines matching "Why is X stock surging/skyrocketing/jumping/rising/gaining/soaring" are written AFTER the move already happened — the opportunity is gone. Return hold.
5. REJECT articles where the headline says shares are "trading higher after..." or "trading lower after..." — this describes a price that already moved. Return hold.
6. REJECT routine scheduled data releases: monthly auto sales reports, CEO/shareholder letters without specific new surprises, recurring supply/demand reports. These are already priced in by the market.
7. STALE NEWS WARNING: if news_age_hours > 2.0, the market has likely already fully priced in this catalyst. Lower confidence significantly. If news_age_hours > 4.0, return hold unless the catalyst is an exceptionally rare binary event (e.g., FDA approval).
8. MARKET DIRECTION CHECK: if the article text implies the price has already made a large move, be skeptical. Chasing an extended move has poor risk/reward. Lower confidence when the article implies "up 9%" or "surging 25%".
9. Same-day duplicate: if the same underlying event (same earnings release, same FDA approval) is being re-reported in a follow-up article, return hold.

Return ONLY a valid JSON object, nothing else. Use exactly one of these formats:
{{"action": "buy", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0-1.0, "hold_hours": int}}
{{"action": "short", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0-1.0, "hold_hours": int}}
{{"action": "sell", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0, "hold_hours": 0}}
{{"action": "hold", "ticker": null, "reasoning": "one sentence", "confidence": 0.0, "hold_hours": 0}}

confidence: your estimated probability that the price moves in the intended direction within hold_hours. Be honest — if unsure, return hold.
hold_hours: how many hours the catalyst is expected to remain relevant (1-48).
"""

_VALID_ACTIONS = frozenset({"buy", "short", "sell", "hold"})


@dataclass
class Decision:
    action: Literal["buy", "short", "sell", "hold"]
    ticker: str | None
    reasoning: str
    confidence: float = 0.0
    hold_hours: int = 0


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
                confidence=float(data.get("confidence", 0.0)),
                hold_hours=int(data.get("hold_hours", 0)),
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
        else:  # chatgpt
            self._provider = ChatGPTProvider(config.openai_api_key, config.openai_model)

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
        news_age_hours: float = 0.0,
    ) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
            shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
            news_age_hours=news_age_hours,
        )
        try:
            result = await self._provider.complete(prompt)
            return _parse_response(result.text)
        except ValueError as e:
            logger.error("LLM parse error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"parse error: {e}")
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"api error: {e}")
