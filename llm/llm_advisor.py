import json
import logging
import time
from dataclasses import dataclass
from typing import Literal

from config import Config
from llm.pricing import compute_cost
from llm.providers import ChatGPTProvider

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide the best action.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}
News age: {news_age_hours:.1f} hours since publication
Entry-quality precheck: {symbol_entry_context}

Currently held long positions: {held_tickers}
Currently held short positions: {shorted_tickers}

Actions available:
- buy: open a long position (bet the price goes UP). Only for tickers in the news.
- short: open a short position (bet the price goes DOWN). Only for tickers in the news.
- sell: close an open long OR short position. Only for tickers you currently hold.
- hold: do nothing.

Rules - evaluate each one before deciding:
1. Only act on tickers directly mentioned in the news.
2. Do not open a long and short on the same ticker simultaneously.
3. REQUIRE a hard catalyst. Valid hard catalysts are: quantified earnings/guidance surprise, FDA/EMA approval or rejection, clinical trial primary-endpoint success/failure, signed M&A with deal value, major contract/order/award with dollar value, or a material regulatory/legal decision with financial amount. Return hold for anything else.
4. REJECT retrospective move-explanation articles. Headlines matching "Why is X stock surging/skyrocketing/jumping/rising/gaining/soaring" are written AFTER the move already happened - the opportunity is gone. Return hold.
5. REJECT articles where the headline says shares are "trading higher after..." or "trading lower after..." - this describes a price that already moved. Return hold.
6. REJECT routine scheduled data releases: monthly auto sales reports, CEO/shareholder letters without specific new surprises, recurring supply/demand reports. These are already priced in by the market.
7. STALE NEWS WARNING: if news_age_hours > 2.0, the market has likely already fully priced in this catalyst. Lower confidence significantly. If news_age_hours > 4.0, return hold unless the catalyst is an exceptionally rare binary event (e.g., FDA approval).
8. MARKET DIRECTION CHECK: if the article text implies the price has already made a large move, be skeptical. Chasing an extended move has poor risk/reward. Lower confidence when the article implies "up 9%" or "surging 25%".
9. Same-day duplicate: if the same underlying event (same earnings release, same FDA approval) is being re-reported in a follow-up article, return hold.
10. REJECT soft partnership/collaboration/investment headlines unless the article gives direct financial materiality for the ticker being traded (e.g., revenue, order/contract value, earnings/guidance impact, acquisition/settlement value).
11. REJECT analyst upgrades/downgrades, price-target changes, watchlists, commentary, narrative/opinion articles, "may/could benefit" articles, and vague positive or negative sentiment.
12. Shorts are allowed only for highly liquid large caps or liquid ETFs. Never short low-price, low-volume, or hard-to-borrow names.
13. Prefer very short holds for news momentum. For new buy/short decisions, set hold_hours to 1 unless the catalyst is a rare binary event.
14. Ticker selection matters. If several tickers are mentioned, choose the most directly affected liquid common stock or liquid ETF. Avoid small caps, newly listed names, proxy/derivative tickers, crypto symbols, and unfamiliar symbols when a cleaner liquid ticker is available.
15. For buy/short decisions, do not choose a ticker marked blocked by the entry-quality precheck. If the only directly affected ticker is blocked for low price, missing quote, missing spread, or wide spread, return hold.
16. Do not choose a ticker likely to trade below the configured minimum price. If unsure and there is no clearly liquid alternative, return hold.

Return ONLY a valid JSON object, nothing else. Use exactly one of these formats:
{{"action": "buy", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0-1.0, "hold_hours": int}}
{{"action": "short", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0-1.0, "hold_hours": int}}
{{"action": "sell", "ticker": "SYMBOL", "reasoning": "one sentence", "confidence": 0.0, "hold_hours": 0}}
{{"action": "hold", "ticker": null, "reasoning": "one sentence", "confidence": 0.0, "hold_hours": 0}}

confidence: your estimated probability that the price moves in the intended direction within hold_hours. Be honest - if unsure, return hold.
hold_hours: how many hours the catalyst is expected to remain relevant (1-48).
"""

_VALID_ACTIONS = frozenset({"buy", "short", "sell", "hold"})
_PROVIDER_NAME = "chatgpt"


@dataclass
class Decision:
    action: Literal["buy", "short", "sell", "hold"]
    ticker: str | None
    reasoning: str
    confidence: float = 0.0
    hold_hours: int = 0
    provider: str = _PROVIDER_NAME
    latency_sec: float | None = None
    cost_usd: float | None = None


def _build_prompt(
    *,
    headline: str,
    summary: str,
    symbols: list[str],
    held_tickers: set[str],
    shorted_tickers: set[str],
    news_age_hours: float = 0.0,
    symbol_entry_context: str = "not checked",
) -> str:
    return _PROMPT_TEMPLATE.format(
        headline=headline,
        summary=summary or "(no summary)",
        symbols=", ".join(symbols) if symbols else "none",
        held_tickers=", ".join(held_tickers) if held_tickers else "none",
        shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
        news_age_hours=news_age_hours,
        symbol_entry_context=symbol_entry_context or "not checked",
    )


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
        self._model = config.openai_model
        self._provider = ChatGPTProvider(config.openai_api_key, config.openai_model)

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
        news_age_hours: float = 0.0,
        symbol_entry_context: str = "not checked",
    ) -> Decision:
        prompt = _build_prompt(
            headline=headline,
            summary=summary,
            symbols=symbols,
            held_tickers=set(held_tickers),
            shorted_tickers=set(shorted_tickers),
            news_age_hours=news_age_hours,
            symbol_entry_context=symbol_entry_context,
        )
        start = time.monotonic()
        try:
            result = await self._provider.complete(prompt)
            decision = _parse_response(result.text)
            decision = _validate_decision_symbols(decision, symbols, held_tickers, shorted_tickers)
            decision.latency_sec = time.monotonic() - start
            decision.cost_usd = compute_cost(self._model, result.input_tokens, result.output_tokens)
            return decision
        except ValueError as e:
            logger.error("LLM parse error: %s", e)
            return Decision(
                action="hold",
                ticker=None,
                reasoning=f"parse error: {e}",
                latency_sec=time.monotonic() - start,
            )
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return Decision(
                action="hold",
                ticker=None,
                reasoning=f"api error: {e}",
                latency_sec=time.monotonic() - start,
            )


def _validate_decision_symbols(
    decision: Decision,
    symbols: list[str],
    held_tickers: set[str],
    shorted_tickers: set[str],
) -> Decision:
    if decision.ticker is None or decision.action == "hold":
        return decision

    decision.ticker = decision.ticker.upper()
    news_symbols = {s.upper() for s in symbols}
    held_or_shorted = {s.upper() for s in held_tickers} | {s.upper() for s in shorted_tickers}

    if decision.action in ("buy", "short") and decision.ticker not in news_symbols:
        return Decision(
            action="hold",
            ticker=None,
            reasoning=f"LLM selected {decision.ticker}, which was not directly mentioned in the news.",
            provider=decision.provider,
            latency_sec=decision.latency_sec,
            cost_usd=decision.cost_usd,
        )
    if decision.action == "sell" and decision.ticker not in held_or_shorted:
        return Decision(
            action="hold",
            ticker=None,
            reasoning=f"LLM selected sell for {decision.ticker}, but that ticker is not currently held.",
            provider=decision.provider,
            latency_sec=decision.latency_sec,
            cost_usd=decision.cost_usd,
        )
    return decision
