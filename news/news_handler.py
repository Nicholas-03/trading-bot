import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from alpaca.data.live import NewsDataStream
from alpaca.trading.client import TradingClient
from llm.llm_advisor import LLMAdvisor
from trading.order_executor import OrderExecutor
from config import Config

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(
        self,
        client: TradingClient,
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

    def _record_news_safe(self, news, headline: str, summary: str, symbols: list[str]) -> int | None:
        if self._db is None:
            return None
        try:
            created_at = getattr(news, "created_at", None)
            ts = created_at.isoformat() if hasattr(created_at, "isoformat") else datetime.now(timezone.utc).isoformat()
            return self._db.record_news(ts, headline, summary, symbols)
        except Exception as exc:
            logger.warning("Analytics news record failed: %s", exc)
            return None

    def _record_decision_safe(self, news_event_id: int | None, decision) -> int | None:
        if self._db is None:
            return None
        try:
            return self._db.record_decision(
                news_event_id=news_event_id,
                ts=datetime.now(timezone.utc).isoformat(),
                action=decision.action,
                ticker=decision.ticker,
                reasoning=decision.reasoning,
                provider=self._config.llm_provider,
                is_primary=True,
            )
        except Exception as exc:
            logger.warning("Analytics decision record failed: %s", exc)
            return None

    def _record_skip_safe(self, decision_id: int | None, reason: str) -> None:
        if self._db is None or decision_id is None:
            return
        try:
            self._db.record_skip(decision_id, reason)
        except Exception as exc:
            logger.warning("Analytics skip record failed: %s", exc)

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
            news_event_id = self._record_news_safe(news, headline, summary, symbols)

            if not symbols:
                logger.debug("No tickers in news event — skipping")
                return

            account = await asyncio.to_thread(self._client.get_account)
            buying_power = float(account.buying_power)
            if buying_power < self._config.trade_amount_usd:
                logger.info(
                    "Insufficient buying power ($%.2f < $%.2f) — skipping LLM call",
                    buying_power, self._config.trade_amount_usd,
                )
                return

            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
            )

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            decision_id = self._record_decision_safe(news_event_id, decision)

            if decision.action == "buy" and decision.ticker:
                await self._executor.buy(decision.ticker, decision_id=decision_id)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(decision.ticker, decision_id=decision_id)
                else:
                    self._record_skip_safe(decision_id, "short_disabled")
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")
