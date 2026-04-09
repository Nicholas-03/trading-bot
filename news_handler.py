import logging
from alpaca.data.live import NewsDataStream
from llm_advisor import LLMAdvisor
from order_executor import OrderExecutor
from config import Config

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(self, config: Config, llm_advisor: LLMAdvisor, order_executor: OrderExecutor) -> None:
        self._config = config
        self._advisor = llm_advisor
        self._executor = order_executor

    async def run(self) -> None:
        while True:
            try:
                stream = NewsDataStream(
                    api_key=self._config.alpaca_api_key,
                    secret_key=self._config.alpaca_secret_key,
                )
                stream.subscribe_news(self._handle_news, "*")
                logger.info("News WebSocket connected — listening for news")
                await stream._run_forever()
            except Exception as e:
                logger.error("News stream error: %s — reconnecting in 5s", e)
                import asyncio
                await asyncio.sleep(5)

    async def _handle_news(self, news) -> None:
        headline = getattr(news, "headline", "")
        summary = getattr(news, "summary", "")
        symbols: list[str] = getattr(news, "symbols", [])

        logger.info("News received: %s | tickers: %s", headline, symbols)

        if not symbols:
            logger.debug("No tickers in news event — skipping")
            return

        decision = self._advisor.analyze(
            headline=headline,
            summary=summary,
            symbols=symbols,
            held_tickers=self._executor._held_tickers,
        )

        logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

        if decision.action == "buy" and decision.ticker:
            self._executor.buy(decision.ticker)
        elif decision.action == "sell" and decision.ticker:
            self._executor.sell(decision.ticker)
