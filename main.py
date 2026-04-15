import asyncio
import logging
from datetime import date, datetime
from rich.logging import RichHandler
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus, OrderStatus
from config import load_config, Config
from trading.order_executor import OrderExecutor
from llm.llm_advisor import LLMAdvisor
from news.news_handler import NewsHandler
from trading.position_monitor import PositionMonitor
from notifications.telegram_notifier import TelegramNotifier, TelegramCommandListener, NoOpNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s — %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)


def _make_trading_client(config: Config) -> TradingClient:
    return TradingClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
        paper=config.paper,
    )


def _load_open_positions(
    client: TradingClient,
) -> tuple[set[str], set[str], dict[str, date]]:
    positions = client.get_all_positions()
    held = {p.symbol for p in positions if p.side.value == "long"}
    shorted = {p.symbol for p in positions if p.side.value == "short"}
    if held:
        logger.info("Resuming with existing long positions: %s", held)
    if shorted:
        logger.info("Resuming with existing short positions: %s", shorted)

    # Seed _open_dates for positions opened today so the PDT guard
    # is not bypassed after a mid-day restart.
    et = pytz.timezone("America/New_York")
    today = datetime.now(et).date()
    today_start_et = et.localize(datetime.combine(today, datetime.min.time()))
    # QueryOrderStatus only has OPEN/CLOSED/ALL; CLOSED covers filled+cancelled+expired.
    # We filter to OrderStatus.FILLED below to exclude non-fills for currently-held symbols.
    orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start_et, limit=500)
    )
    open_today = {
        o.symbol
        for o in orders
        if o.status == OrderStatus.FILLED and o.symbol in (held | shorted)
    }
    open_dates: dict[str, date] = {symbol: today for symbol in open_today}
    if open_dates:
        logger.info("Seeding PDT open_dates from today's fills: %s", set(open_dates))

    return held, shorted, open_dates


async def main() -> None:
    config = load_config()
    client = _make_trading_client(config)
    held_tickers, shorted_tickers, open_dates = _load_open_positions(client)

    if config.telegram_enabled:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        notifier = NoOpNotifier()

    order_executor = OrderExecutor(client, config, held_tickers, shorted_tickers, notifier, open_dates=open_dates)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(client, config, llm_advisor, order_executor)
    position_monitor = PositionMonitor(client, config, order_executor, notifier)

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
