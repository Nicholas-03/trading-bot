# trading/position_monitor.py
import asyncio
import logging
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
import pytz
from trading.tradier_client import TradierClient
from trading.order_executor import OrderExecutor
from notifications.telegram_notifier import Notifier
from config import Config

if TYPE_CHECKING:
    from analytics.db import TradeDB
    from trading.alpaca_data_client import AlpacaMarketDataClient

logger = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 30
_MAX_ERROR_BACKOFF_SEC = 300


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


def _poll_error_delay(failure_count: int) -> int:
    """Return exponential retry delay after consecutive monitor failures."""
    if failure_count <= 0:
        return _POLL_INTERVAL_SEC
    return min(_MAX_ERROR_BACKOFF_SEC, _POLL_INTERVAL_SEC * (2 ** (failure_count - 1)))


def _should_log_poll_error_at_error(failure_count: int) -> bool:
    """Throttle Telegram ERROR spam while preserving periodic high-signal alerts."""
    return failure_count == 1 or failure_count % 10 == 0


class PositionMonitor:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        order_executor: OrderExecutor,
        notifier: Notifier,
        db: "TradeDB | None" = None,
        market_data_client: "AlpacaMarketDataClient | None" = None,
    ) -> None:
        self._client = client
        self._executor = order_executor
        self._notifier = notifier
        self._db = db
        self._market_data_client = market_data_client
        self._last_report_date: date | None = None

    async def run(self) -> None:
        await asyncio.gather(self._position_loop(), self._report_loop())

    async def _position_loop(self) -> None:
        failure_count = 0
        while True:
            await asyncio.sleep(_POLL_INTERVAL_SEC if failure_count == 0 else _poll_error_delay(failure_count))
            try:
                await self._check_positions()
                if failure_count:
                    logger.info("Position monitor recovered after %d failed poll(s)", failure_count)
                failure_count = 0
            except Exception:
                failure_count += 1
                if _should_log_poll_error_at_error(failure_count):
                    logger.exception("Position monitor poll failed")
                else:
                    logger.warning(
                        "Position monitor poll failed (%d consecutive); retrying in %ss",
                        failure_count,
                        _poll_error_delay(failure_count),
                    )

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
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        fallback_buys, fallback_sells, fallback_pnl = self._executor.daily_summary(today)
        buys = fallback_buys
        try:
            broker_buys, broker_activity_sells = self._client.trade_activity_summary_for_date(today)
            if broker_buys or broker_activity_sells:
                buys = broker_buys
        except Exception as exc:
            logger.warning("Tradier account history unavailable for EOD buy count; using analytics DB: %s", exc)
        try:
            broker_sells, broker_pnl = self._client.gain_loss_summary_for_close_date(today)
            return buys, broker_sells, broker_pnl
        except Exception as exc:
            logger.warning("Tradier gain/loss unavailable for EOD P&L; using analytics DB: %s", exc)
            return fallback_buys, fallback_sells, fallback_pnl

    def _fetch_weekly_data(self) -> tuple[int, int, float]:
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        week_monday = today - timedelta(days=today.weekday())
        week_end = week_monday + timedelta(days=6)
        fallback_buys, fallback_sells, fallback_pnl = self._executor.weekly_summary(week_monday)
        buys = fallback_buys
        try:
            broker_buys, broker_activity_sells = self._client.trade_activity_summary_for_date_range(week_monday, week_end)
            if broker_buys or broker_activity_sells:
                buys = broker_buys
        except Exception as exc:
            logger.warning("Tradier account history unavailable for weekly buy count; using analytics DB: %s", exc)
        try:
            broker_sells, broker_pnl = self._client.gain_loss_summary_for_close_date_range(week_monday, week_end)
            return buys, broker_sells, broker_pnl
        except Exception as exc:
            logger.warning("Tradier gain/loss unavailable for weekly P&L; using analytics DB: %s", exc)
            return fallback_buys, fallback_sells, fallback_pnl

    async def _check_positions(self) -> None:
        await self._record_account_value_snapshot()
        positions = await asyncio.to_thread(self._client.get_all_positions)
        live_symbols = {pos.symbol for pos in positions}

        # Confirm tickers that Tradier no longer returns as part of a manual close
        for ticker in self._executor.pending_close - live_symbols:
            self._executor.confirm_closed(ticker)
            logger.info("Confirmed closed: %s no longer in Tradier positions", ticker)

        # A close may be deferred while Tradier settles bracket leg cancellations.
        # Once those blocking orders disappear, clear the guard so expired timed
        # exits can retry the manual market close.
        for ticker in self._executor.pending_close & live_symbols:
            if await self._executor.close_deferred_ready(ticker):
                self._executor.confirm_closed(ticker)
                logger.info("Deferred close ready to retry for %s", ticker)

        # Detect positions closed by a protective bracket (held but disappeared, not manually closed).
        # Exclude pending_fill tickers: their entry is not fully confirmed/bracketed yet, so
        # treating them as bracket-closed would be a false positive.
        otoco_closed = self._executor.held_tickers - live_symbols - self._executor.pending_close - self._executor.pending_fill
        if otoco_closed:
            quotes = await self._latest_prices(list(otoco_closed))
            for ticker in otoco_closed:
                logger.info("Position %s no longer in Tradier - protective bracket fired", ticker)
                await self._executor.handle_bracket_close(ticker, quotes.get(ticker))

        # Early/risk exits for live positions. Longs also have broker-side
        # brackets; shorts rely on this monitor for stop/take-profit exits.
        live_tracked = (
            (self._executor.held_tickers | self._executor.shorted_tickers)
            & live_symbols
            - self._executor.pending_close
            - self._executor.pending_fill
        )
        if live_tracked:
            quotes = await self._latest_prices(list(live_tracked))
            for ticker, price in quotes.items():
                exit_reason = self._executor.update_price_for_exit_signal(ticker, price)
                if exit_reason:
                    await self._executor.sell(ticker, exit_reason=exit_reason)

        # Detect short positions closed externally (not via this bot's sell path)
        external_short_closed = self._executor.shorted_tickers - live_symbols - self._executor.pending_close
        for ticker in external_short_closed:
            logger.warning("Short position %s disappeared from Tradier — reconciling state", ticker)
            await self._executor.sell(ticker, exit_reason="external_close")

        # Log current state of all hold windows, then close any expired ones
        now_utc = datetime.now(timezone.utc)
        for ticker, (opened_at, hold_hours, expiry) in self._executor.hold_windows.items():
            age_hours = (now_utc - opened_at).total_seconds() / 3600
            logger.info(
                "TIMED EXIT CHECK: ticker=%s opened_at=%s hold_hours=%d age_hours=%.2f expires=%s",
                ticker, opened_at.isoformat(), hold_hours, age_hours, expiry.isoformat(),
            )
        expired = [
            ticker for ticker in self._executor.expired_hold_tickers()
            if ticker not in self._executor.pending_close
        ]
        if expired:
            clock = await asyncio.to_thread(self._client.get_clock)
            if not clock.is_open:
                logger.info(
                    "TIMED EXIT DEFERRED: market closed; will retry when open for %s",
                    sorted(expired),
                )
                return
        for ticker in expired:
            logger.info("TIMED EXIT TRIGGERED: ticker=%s", ticker)
            await self._executor.sell(ticker, exit_reason="hold_hours")

    async def _record_account_value_snapshot(self) -> None:
        if self._db is None:
            return
        try:
            value = await asyncio.to_thread(self._client.get_account_total_value)
            ts = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(self._db.record_account_value, ts, value)
        except Exception as exc:
            logger.warning("Account value snapshot unavailable: %s", exc)

    async def _latest_prices(self, symbols: list[str]) -> dict[str, float]:
        if self._market_data_client is None or not symbols:
            logger.warning("Position price data unavailable: Alpaca market data client missing")
            return {}
        try:
            prices = await asyncio.to_thread(self._market_data_client.get_latest_prices, symbols)
            logger.info("POSITION PRICE DATA: provider=alpaca symbols=%s prices=%s", symbols, prices)
            return prices
        except Exception as exc:
            logger.warning("Position price data unavailable from Alpaca for %s: %s", symbols, exc)
            return {}
