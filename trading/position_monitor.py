# trading/position_monitor.py
import asyncio
import logging
from datetime import date, datetime
import pytz
from trading.tradier_client import TradierClient
from trading.order_executor import OrderExecutor
from notifications.telegram_notifier import Notifier
from config import Config

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


def _should_fire_report(now_et: datetime, last_report_date: date | None) -> bool:
    """Return True if the EOD/weekly report should fire now.

    Fires during the 16:00–16:01 ET window on weekdays, at most once per calendar day.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be timezone-aware (ET)")
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if now_et.hour != 16 or now_et.minute > 1:
        return False
    return last_report_date != now_et.date()


class PositionMonitor:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        order_executor: OrderExecutor,
        notifier: Notifier,
    ) -> None:
        self._client = client
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor
        self._notifier = notifier
        self._last_report_date: date | None = None

    async def run(self) -> None:
        await asyncio.gather(self._position_loop(), self._report_loop())

    async def _position_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_report()
            except Exception:
                logger.exception("Report loop error")

    async def _check_report(self) -> None:
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if not _should_fire_report(now, self._last_report_date):
            return

        today = now.date()
        buys, sells, pnl = self._fetch_eod_data()
        await self._notifier.notify_eod_report(buys, sells, pnl)
        self._last_report_date = today
        logger.info("EOD report sent: buys=%d sells=%d pnl=%.2f", buys, sells, pnl)

        if today.weekday() == 4:  # Friday
            w_buys, w_sells, w_pnl = self._fetch_weekly_data()
            await self._notifier.notify_weekly_report(w_buys, w_sells, w_pnl)
            logger.info("Weekly report sent: buys=%d sells=%d pnl=%.2f", w_buys, w_sells, w_pnl)

    def _fetch_eod_data(self) -> tuple[int, int, float]:
        return self._executor.daily_summary()

    def _fetch_weekly_data(self) -> tuple[int, int, float]:
        return self._executor.weekly_summary()

    async def _check_positions(self) -> None:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        live_symbols = {pos.symbol for pos in positions}

        # Confirm tickers that Tradier no longer returns
        for ticker in self._executor.pending_close - live_symbols:
            self._executor.confirm_closed(ticker)
            logger.info("Confirmed closed: %s no longer in Tradier positions", ticker)

        open_positions = [
            pos for pos in positions if pos.symbol not in self._executor.pending_close
        ]
        if not open_positions:
            return

        symbols = [pos.symbol for pos in open_positions]
        quotes = await asyncio.to_thread(self._client.get_quotes, symbols)

        for pos in open_positions:
            try:
                ticker = pos.symbol
                if pos.qty < 0:
                    logger.debug("Skipping short position %s — P&L monitoring not supported for shorts", ticker)
                    continue
                qty = abs(pos.qty)
                if qty == 0:
                    continue
                # cost_basis is the total cost (e.g. $300 for 2 shares at $150 avg)
                entry = pos.cost_basis / qty
                if entry == 0.0:
                    logger.warning("Skipping %s — entry price is zero", ticker)
                    continue

                current = quotes.get(ticker)
                if current is None:
                    logger.warning("No quote for %s — skipping", ticker)
                    continue

                pnl = compute_pnl_pct(entry, current)
                pnl_usd = (current - entry) * qty

                if pnl <= -self._stop_loss:
                    logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd, exit_reason="stop_loss")
                elif pnl >= self._take_profit:
                    logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd, exit_reason="take_profit")
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
