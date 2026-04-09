import asyncio
import logging
from config import load_config, Config
from order_executor import OrderExecutor
from llm_advisor import LLMAdvisor
from news_handler import NewsHandler
from position_monitor import PositionMonitor
from alpaca.trading.client import TradingClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _load_open_positions(config: Config) -> tuple[set[str], set[str]]:
    client = TradingClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
        paper=config.paper,
    )
    positions = client.get_all_positions()
    held = {p.symbol for p in positions if p.side.value == "long"}
    shorted = {p.symbol for p in positions if p.side.value == "short"}
    if held:
        logger.info("Resuming with existing long positions: %s", held)
    if shorted:
        logger.info("Resuming with existing short positions: %s", shorted)
    return held, shorted


async def main() -> None:
    config = load_config()
    held_tickers, shorted_tickers = _load_open_positions(config)

    order_executor = OrderExecutor(config, held_tickers, shorted_tickers)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(config, llm_advisor, order_executor)
    position_monitor = PositionMonitor(config, order_executor)

    logger.info("Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
                config.paper, config.trade_amount_usd,
                config.stop_loss_pct * 100, config.take_profit_pct * 100)

    try:
        await asyncio.gather(
            news_handler.run(),
            position_monitor.run(),
        )
    except asyncio.CancelledError:
        logger.info("Bot shutting down")


if __name__ == "__main__":
    asyncio.run(main())
