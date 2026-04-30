# news/news_handler.py
import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from alpaca.data.live import NewsDataStream
from trading.tradier_client import TradierClient
from llm.llm_advisor import LLMAdvisor
from trading.order_executor import OrderExecutor
from config import Config

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        llm_advisor: LLMAdvisor,
        order_executor: OrderExecutor,
        db: "TradeDB | None" = None,
    ) -> None:
        self._client = client
        self._config = config
        self._advisor = llm_advisor
        self._executor = order_executor
        self._db = db

    async def run(self) -> None:
        while True:
            try:
                stream = NewsDataStream(
                    api_key=self._config.alpaca_api_key,
                    secret_key=self._config.alpaca_secret_key,
                )
                stream.subscribe_news(self._handle_news, "*")
                logger.info("News WebSocket connected — listening for news")
                # alpaca-py's public stream.run() calls asyncio.run() internally,
                # which conflicts with our event loop. We call _run_forever() directly
                # so the stream runs inside the same asyncio.gather loop as the
                # position monitor. Revisit if alpaca-py adds an async-native entry point.
                await stream._run_forever()
            except Exception:
                logger.exception("News stream error — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_news(self, news) -> None:
        try:
            clock = await asyncio.to_thread(self._client.get_clock)
            if not clock.is_open:
                logger.debug("Market closed — skipping news event")
                return
            headline = getattr(news, "headline", "")
            summary = getattr(news, "summary", "")
            symbols: list[str] = getattr(news, "symbols", [])

            logger.info("News received: %s | tickers: %s", headline, symbols)

            if not symbols:
                logger.debug("No tickers in news event — skipping")
                return

            news_event_id: int | None = None
            if self._db is not None:
                try:
                    news_ts = datetime.now(timezone.utc).isoformat()
                    news_event_id = await asyncio.to_thread(
                        self._db.record_news, news_ts, headline, summary, symbols
                    )
                except Exception as db_err:
                    logger.warning(
                        "Failed to record news event in analytics DB: %s — decision and trade will be unlinked",
                        db_err,
                    )

            # Capture decision timestamp before analyzing
            decision_monotonic = time.monotonic()
            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
            )

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            decision_id: int | None = None
            if self._db is not None and news_event_id is not None:
                try:
                    decision_ts = datetime.now(timezone.utc).isoformat()
                    decision_id = await asyncio.to_thread(
                        self._db.record_decision,
                        news_event_id, decision_ts, decision.action, decision.ticker, decision.reasoning,
                    )
                except Exception as db_err:
                    logger.warning("Failed to record LLM decision in analytics DB: %s", db_err)

            if decision.action == "buy" and decision.ticker:
                await self._executor.buy(decision.ticker, decision_id=decision_id, decision_monotonic=decision_monotonic)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(decision.ticker, decision_id=decision_id, decision_monotonic=decision_monotonic)
                else:
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")
