# news/news_handler.py
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from alpaca.data.live import NewsDataStream

from config import Config
from llm.llm_advisor import LLMAdvisor
from news.filters import (
    compute_news_age_hours,
    is_hard_catalyst_news,
    is_retrospective_headline,
    is_routine_news,
    is_soft_partnership_without_materiality,
    is_vague_or_analyst_news,
)
from trading.order_executor import OrderExecutor
from trading.tradier_client import TradierClient

if TYPE_CHECKING:
    from analytics.db import TradeDB
    from trading.alpaca_data_client import AlpacaMarketDataClient

logger = logging.getLogger(__name__)


def _effective_hold_hours(requested: int, config: Config) -> int:
    hold_hours = requested if requested > 0 else config.default_hold_hours
    return max(1, min(hold_hours, config.max_hold_hours))


def _positive_float(value) -> float | None:
    if value is None:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _format_entry_precheck_context(
    symbols: list[str],
    snapshots: dict,
    *,
    min_trade_price: float,
    max_entry_spread_pct: float,
) -> str:
    clean_symbols: list[str] = []
    seen: set[str] = set()
    for symbol in symbols:
        clean = str(symbol).strip().upper()
        if clean and clean not in seen:
            clean_symbols.append(clean)
            seen.add(clean)

    if not clean_symbols:
        return "not checked (no symbols)"

    tradable: list[str] = []
    blocked: list[str] = []
    normalized_snapshots = {str(k).upper(): v for k, v in snapshots.items()}
    for symbol in clean_symbols:
        snapshot = normalized_snapshots.get(symbol)
        if snapshot is None:
            blocked.append(f"{symbol}: no_quote")
            continue

        price = _positive_float(getattr(snapshot, "entry_price", None))
        spread_pct = getattr(snapshot, "spread_pct", None)
        if price is None:
            blocked.append(f"{symbol}: no_quote")
        elif price < min_trade_price:
            blocked.append(f"{symbol}: low_price price=${price:.2f}")
        elif spread_pct is None:
            blocked.append(f"{symbol}: entry_spread_unavailable price=${price:.2f}")
        elif spread_pct > max_entry_spread_pct:
            blocked.append(
                f"{symbol}: wide_spread price=${price:.2f} "
                f"spread={spread_pct * 100:.2f}%"
            )
        else:
            tradable.append(f"{symbol}: price=${price:.2f} spread={spread_pct * 100:.2f}%")

    tradable_text = "; ".join(tradable) if tradable else "none"
    blocked_text = "; ".join(blocked) if blocked else "none"
    return (
        f"tradable now: {tradable_text}. "
        f"blocked now: {blocked_text}. "
        f"minimum price=${min_trade_price:.2f}; max spread={max_entry_spread_pct * 100:.2f}%."
    )


class NewsHandler:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        llm_advisor: LLMAdvisor,
        order_executor: OrderExecutor,
        db: "TradeDB | None" = None,
        market_data_client: "AlpacaMarketDataClient | None" = None,
    ) -> None:
        self._client = client
        self._config = config
        self._advisor = llm_advisor
        self._executor = order_executor
        self._db = db
        self._market_data_client = market_data_client

    async def run(self) -> None:
        while True:
            try:
                stream = NewsDataStream(
                    api_key=self._config.alpaca_api_key,
                    secret_key=self._config.alpaca_secret_key,
                )
                stream.subscribe_news(self._handle_news, "*")
                logger.info("News WebSocket connected - listening for news")
                # alpaca-py's public stream.run() calls asyncio.run() internally,
                # which conflicts with our event loop. We call _run_forever() directly
                # so the stream runs inside the same asyncio.gather loop as the
                # position monitor. Revisit if alpaca-py adds an async-native entry point.
                await stream._run_forever()
            except Exception:
                logger.exception("News stream error - reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_news(self, news) -> None:
        try:
            clock = await asyncio.to_thread(self._client.get_clock)
            if not clock.is_open:
                logger.debug("Market closed - skipping news event")
                return
            headline = getattr(news, "headline", "")
            summary = getattr(news, "summary", "")
            symbols: list[str] = getattr(news, "symbols", [])

            logger.info("News received: %s | tickers: %s", headline, symbols)

            if not symbols:
                logger.debug("No tickers in news event - skipping")
                return

            if is_retrospective_headline(headline):
                logger.info("SKIP [retrospective_headline_block] %s", headline[:100])
                return

            if is_routine_news(headline):
                logger.info("SKIP [routine_news_block] %s", headline[:100])
                return

            if is_vague_or_analyst_news(headline, summary):
                logger.info("SKIP [vague_or_analyst_news_block] %s", headline[:100])
                return

            if (
                self._config.block_soft_partnership_news
                and is_soft_partnership_without_materiality(headline, summary)
            ):
                logger.info("SKIP [soft_partnership_materiality_block] %s", headline[:100])
                return

            if self._config.require_hard_catalyst_news and not is_hard_catalyst_news(headline, summary):
                logger.info("SKIP [hard_catalyst_required_block] %s", headline[:100])
                return

            article_ts = getattr(news, "created_at", None)
            if not isinstance(article_ts, datetime):
                article_ts = None
            age_hours = compute_news_age_hours(article_ts) if article_ts is not None else 0.0
            stale_threshold = self._config.news_stale_hours
            if age_hours > stale_threshold:
                logger.info(
                    "SKIP [stale_news_block] %s - news is %.1fh old (threshold %.1fh)",
                    headline[:80], age_hours, stale_threshold,
                )
                return

            news_event_id: int | None = None
            if self._db is not None:
                try:
                    news_ts = (article_ts or datetime.now(timezone.utc)).isoformat()
                    news_event_id = await asyncio.to_thread(
                        self._db.record_news, news_ts, headline, summary, symbols
                    )
                except Exception as db_err:
                    logger.warning(
                        "Failed to record news event in analytics DB: %s - decision and trade will be unlinked",
                        db_err,
                    )

            decision_monotonic = time.monotonic()
            symbol_entry_context = await self._build_entry_precheck_context(symbols)
            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
                news_age_hours=age_hours,
                symbol_entry_context=symbol_entry_context,
            )

            logger.info(
                "LLM decision [%s]: %s %s confidence=%.2f - %s",
                decision.provider, decision.action, decision.ticker,
                decision.confidence, decision.reasoning,
            )

            if decision.action in ("buy", "short"):
                capped_hold = _effective_hold_hours(decision.hold_hours, self._config)
                if capped_hold != decision.hold_hours:
                    logger.info(
                        "Adjusted hold_hours for %s %s from %s to %s",
                        decision.action, decision.ticker, decision.hold_hours, capped_hold,
                    )
                    decision.hold_hours = capped_hold

            decision_id: int | None = None
            if self._db is not None and news_event_id is not None:
                try:
                    decision_ts = datetime.now(timezone.utc).isoformat()
                    decision_id = await asyncio.to_thread(
                        self._db.record_decision,
                        news_event_id,
                        decision_ts,
                        decision.action,
                        decision.ticker,
                        decision.reasoning,
                        decision.confidence,
                        decision.hold_hours,
                        decision.provider,
                        decision.latency_sec,
                        decision.cost_usd,
                        True,
                    )
                except Exception as db_err:
                    logger.warning("Failed to record LLM decision in analytics DB: %s", db_err)

            if decision.action in ("buy", "short") and decision.confidence < self._config.min_confidence:
                logger.info(
                    "Skipping %s %s - confidence %.2f below threshold %.2f",
                    decision.action, decision.ticker, decision.confidence, self._config.min_confidence,
                )
                if self._db is not None and decision_id is not None:
                    try:
                        await asyncio.to_thread(self._db.record_skip, decision_id, "confidence_below_threshold")
                    except Exception as db_err:
                        logger.warning("Failed to record skip reason for decision %s: %s", decision_id, db_err)
                return

            if decision.action == "buy" and decision.ticker:
                logger.info(
                    "EXECUTING BUY: provider=%s ticker=%s confidence=%.2f decision_id=%s",
                    decision.provider, decision.ticker, decision.confidence, decision_id,
                )
                await self._executor.buy(
                    decision.ticker,
                    decision_id=decision_id,
                    decision_monotonic=decision_monotonic,
                    hold_hours=decision.hold_hours,
                )
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(
                        decision.ticker,
                        decision_id=decision_id,
                        decision_monotonic=decision_monotonic,
                        hold_hours=decision.hold_hours,
                    )
                else:
                    logger.info("Short selling disabled - skipping short for %s", decision.ticker)
                    if self._db is not None and decision_id is not None:
                        try:
                            await asyncio.to_thread(self._db.record_skip, decision_id, "short_disabled")
                        except Exception as db_err:
                            logger.warning("Failed to record skip reason for decision %s: %s", decision_id, db_err)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")

    async def _build_entry_precheck_context(self, symbols: list[str]) -> str:
        if self._market_data_client is None:
            return "not checked (market data client unavailable)"
        try:
            snapshots = await asyncio.to_thread(self._market_data_client.get_snapshots, symbols)
            context = _format_entry_precheck_context(
                symbols,
                snapshots,
                min_trade_price=self._config.min_trade_price,
                max_entry_spread_pct=self._config.max_entry_spread_pct,
            )
            logger.info("ENTRY PRECHECK CONTEXT: %s", context)
            return context
        except Exception as exc:
            logger.warning("Entry precheck unavailable for %s: %s", symbols, exc)
            return "not checked (Alpaca precheck failed)"
