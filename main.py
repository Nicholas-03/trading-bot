import asyncio
import logging
from rich.logging import RichHandler
from config import load_config, Config
from trading.order_executor import OrderExecutor
from llm.llm_advisor import LLMAdvisor
from news.news_handler import NewsHandler
from trading.position_monitor import PositionMonitor
from alpaca.trading.client import TradingClient
from notifications.telegram_notifier import TelegramNotifier, TelegramCommandListener, NoOpNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s — %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)


def _load_open_positions(config: Config) -> tuple[TradingClient, set[str], set[str]]:
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
    return client, held, shorted


async def main() -> None:
    config = load_config()
    client, held_tickers, shorted_tickers = _load_open_positions(config)

    if config.telegram_enabled:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        notifier = NoOpNotifier()

    order_executor = OrderExecutor(client, config, held_tickers, shorted_tickers, notifier)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(config, llm_advisor, order_executor)
    position_monitor = PositionMonitor(client, config, order_executor)

    coroutines = [news_handler.run(), position_monitor.run()]
    command_listener = None
    if config.telegram_enabled:
        command_listener = TelegramCommandListener(
            config.telegram_bot_token, config.telegram_chat_id, order_executor
        )
        coroutines.append(command_listener.run())

    logger.info("Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
                config.paper, config.trade_amount_usd,
                config.stop_loss_pct * 100, config.take_profit_pct * 100)

    try:
        await asyncio.gather(*coroutines)
    except asyncio.CancelledError:
        logger.info("Bot shutting down")
    finally:
        await notifier.aclose()
        if command_listener:
            await command_listener.aclose()


if __name__ == "__main__":
    asyncio.run(main())
