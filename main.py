# main.py
import asyncio
import logging
import os
from datetime import datetime, timezone
from rich.logging import RichHandler
from trading.tradier_client import TradierClient
from config import load_config, Config
from trading.order_executor import OrderExecutor
from trading.alpaca_data_client import AlpacaMarketDataClient
from llm.llm_advisor import LLMAdvisor
from news.news_handler import NewsHandler
from trading.position_monitor import PositionMonitor
from notifications.telegram_notifier import TelegramNotifier, TelegramCommandListener, TelegramLogHandler, NoOpNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s — %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _make_tradier_client(config: Config) -> TradierClient:
    return TradierClient(
        access_token=config.tradier_access_token,
        account_id=config.tradier_account_id,
        paper=config.tradier_paper,
    )


def _reconcile_stale_trades(
    client: TradierClient,
    db: "TradeDB",  # type: ignore[name-defined]
    stale_trades: list[dict],
) -> None:
    """Close DB trades whose broker position is gone but closed_at was never written.

    Fetches account order history once, then for each stale trade looks for the most
    recent filled close-side order matching the ticker.  Updates the DB with whatever
    exit data is recoverable; if no fill is found, marks closed with a sentinel reason
    so the row is not endlessly re-reconciled on future restarts.

    What can be backfilled:
    - closed_at      — from order transaction_date if found, else now()
    - exit_price     — from avg_fill_price if found, else NULL
    - pnl_usd/pct   — calculated from entry_price when exit_price is available
    - exit_reason    — inferred from order type (limit→take_profit, stop→stop_loss)

    What cannot be recovered:
    - Exact fill timestamp if Tradier order history has already rolled off (sandbox: ~7 days)
    - Partial fills (bot always uses qty=full position, so this should not occur)
    """
    try:
        orders = client.get_account_orders()
    except Exception as exc:
        logger.warning("Reconciliation: failed to fetch account orders: %s", exc)
        orders = []

    now = datetime.now(timezone.utc).isoformat()
    for t in stale_trades:
        ticker = t["ticker"]
        trade_id = t["id"]
        entry_price = float(t.get("entry_price") or 0.0)
        qty = int(t.get("qty") or 0)
        is_short = t.get("side") == "short"
        close_side = "buy_to_cover" if is_short else "sell"
        opened_at = _parse_iso_dt(t.get("opened_at"))

        fills = [
            o for o in orders
            if o.symbol == ticker
            and o.side == close_side
            and o.status == "filled"
            and o.avg_fill_price is not None
            and (
                opened_at is None
                or (fill_dt := _parse_iso_dt(o.filled_at)) is None
                or fill_dt >= opened_at
            )
        ]
        fills.sort(key=lambda o: o.filled_at or "", reverse=True)

        if fills:
            best = fills[0]
            exit_price = best.avg_fill_price
            if best.order_type == "limit":
                exit_reason = "take_profit"
            elif best.order_type in ("stop", "stop_limit"):
                exit_reason = "stop_loss"
            else:
                exit_reason = "bracket_order"
            closed_at = best.filled_at or now
            if entry_price and qty:
                price_delta = entry_price - exit_price if is_short else exit_price - entry_price
                pnl_usd = price_delta * qty
                pnl_pct = price_delta / entry_price
            else:
                pnl_usd = None
                pnl_pct = None
            logger.info(
                "Reconciled %s (trade_id=%s): exit=$%.4f reason=%s closed_at=%s",
                ticker, trade_id, exit_price, exit_reason, closed_at,
            )
        else:
            exit_price = None
            pnl_usd = None
            pnl_pct = None
            exit_reason = "reconciled_unknown_exit"
            closed_at = now
            logger.warning(
                "Reconciling %s (trade_id=%s): no filled close order in history — "
                "marking closed with unknown exit; P&L not recoverable",
                ticker, trade_id,
            )

        try:
            db.record_trade_close(trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at)
        except Exception as exc:
            logger.warning("Failed to reconcile trade_id=%s for %s: %s", trade_id, ticker, exc)


def _load_open_positions(client: TradierClient) -> tuple[set[str], set[str]]:
    positions = client.get_all_positions()
    held = {p.symbol for p in positions if p.qty > 0}
    shorted = {p.symbol for p in positions if p.qty < 0}
    if held:
        logger.info("Resuming with existing long positions: %s", held)
    if shorted:
        logger.info("Resuming with existing short positions: %s", shorted)
    return held, shorted


def _record_account_value_snapshot(client: TradierClient, db) -> None:
    try:
        value = client.get_account_total_value()
        db.record_account_value(datetime.now(timezone.utc).isoformat(), value)
        logger.info("Recorded account value snapshot: $%.2f", value)
    except Exception as exc:
        logger.warning("Initial account value snapshot unavailable: %s", exc)


async def main() -> None:
    config = load_config()
    client = _make_tradier_client(config)
    market_data_client = AlpacaMarketDataClient(
        config.alpaca_api_key,
        config.alpaca_secret_key,
        config.alpaca_data_feed,
    )
    db = None
    try:
        held_tickers, shorted_tickers = _load_open_positions(client)

        log_handler: TelegramLogHandler | None = None
        if config.telegram_enabled:
            notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
            log_handler = TelegramLogHandler(
                config.telegram_bot_token,
                config.telegram_chat_id,
                asyncio.get_running_loop(),
            )
            logging.getLogger().addHandler(log_handler)
        else:
            notifier = NoOpNotifier()

        if config.analytics_db_path:
            from analytics.db import TradeDB
            os.makedirs(os.path.dirname(config.analytics_db_path) or ".", exist_ok=True)
            db = TradeDB(config.analytics_db_path)
            logger.info("Analytics DB: %s", config.analytics_db_path)
            _record_account_value_snapshot(client, db)

        order_executor = OrderExecutor(
            client,
            config,
            held_tickers,
            shorted_tickers,
            notifier,
            db,
            market_data_client,
        )
        if db is not None:
            open_trades = db.get_open_trades()
            stale = [t for t in open_trades
                     if t["ticker"] not in held_tickers and t["ticker"] not in shorted_tickers]
            if stale:
                logger.info("Found %d stale DB trade(s) with no live broker position — reconciling", len(stale))
                _reconcile_stale_trades(client, db, stale)
                open_trades = db.get_open_trades()  # re-fetch after reconciliation
            order_executor.seed_from_db(open_trades)
            logger.info("Seeded %d open trade(s) from analytics DB", len(open_trades))
        llm_advisor = LLMAdvisor(config)
        news_handler = NewsHandler(client, config, llm_advisor, order_executor, db)
        position_monitor = PositionMonitor(client, config, order_executor, notifier, db, market_data_client)

        coroutines = [news_handler.run(), position_monitor.run()]
        command_listener = None
        if config.telegram_enabled:
            command_listener = TelegramCommandListener(
                config.telegram_bot_token, config.telegram_chat_id, order_executor
            )
            coroutines.append(command_listener.run())

        logger.info(
            "Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
            config.tradier_paper, config.trade_amount_usd,
            config.stop_loss_pct * 100, config.take_profit_pct * 100,
        )

        try:
            await asyncio.gather(*coroutines)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            await notifier.aclose()
            if command_listener:
                await command_listener.aclose()
            if log_handler:
                logging.getLogger().removeHandler(log_handler)
                await log_handler.aclose()
    finally:
        if db is not None:
            db.close()
        market_data_client.close()
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
