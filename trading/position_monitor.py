import asyncio
import logging
from datetime import date, datetime, timedelta
import pytz
from alpaca.trading.client import TradingClient
from trading.order_executor import OrderExecutor
from config import Config

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


def _should_fire_report(now_et: datetime, last_report_date: date | None) -> bool:
    """Return True if the EOD/weekly report should fire now.

    Fires during the 16:00:00–16:00:59 ET window, at most once per calendar day.
    """
    if now_et.hour != 16 or now_et.minute != 0:
        return False
    return last_report_date != now_et.date()


class PositionMonitor:
    def __init__(self, client: TradingClient, config: Config, order_executor: OrderExecutor) -> None:
        self._client = client
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor

    async def run(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")

    async def _check_positions(self) -> None:
        positions = self._client.get_all_positions()
        for pos in positions:
            try:
                ticker = pos.symbol
                entry = float(pos.avg_entry_price)
                if entry == 0.0:
                    logger.warning("Skipping %s — avg_entry_price is zero", ticker)
                    continue
                current = float(pos.current_price)
                pnl = compute_pnl_pct(entry, current)

                pnl_usd = float(pos.unrealized_pl)
                if pnl <= -self._stop_loss:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping stop-loss close for %s (opened today)", ticker)
                    else:
                        logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
                elif pnl >= self._take_profit:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping take-profit close for %s (opened today)", ticker)
                    else:
                        logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
