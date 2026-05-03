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

        # Confirm tickers that Tradier no longer returns as part of a manual close
        for ticker in self._executor.pending_close - live_symbols:
            self._executor.confirm_closed(ticker)
            logger.info("Confirmed closed: %s no longer in Tradier positions", ticker)

        # Detect positions closed by OTOCO bracket (held but disappeared, not manually closed)
        otoco_closed = self._executor.held_tickers - live_symbols - self._executor.pending_close
        if otoco_closed:
            quotes = await asyncio.to_thread(self._client.get_quotes, list(otoco_closed))
            for ticker in otoco_closed:
                logger.info("Position %s no longer in Tradier — OTOCO bracket fired", ticker)
                await self._executor.handle_bracket_close(ticker, quotes.get(ticker))

        # Detect short positions closed externally (not via this bot's sell path)
        external_short_closed = self._executor.shorted_tickers - live_symbols - self._executor.pending_close
        for ticker in external_short_closed:
            logger.warning("Short position %s disappeared from Tradier — reconciling state", ticker)
            await self._executor.sell(ticker, exit_reason="external_close")

        # Close positions whose hold_hours window has elapsed
        for ticker in self._executor.expired_hold_tickers():
            if ticker not in self._executor.pending_close:
                logger.info("Hold-hours expired for %s — closing position", ticker)
                await self._executor.sell(ticker, exit_reason="hold_hours")
