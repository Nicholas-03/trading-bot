import asyncio
import logging
from datetime import date, datetime, timedelta
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import QueryOrderStatus, OrderStatus, OrderSide
from trading.order_executor import OrderExecutor
from notifications.telegram_notifier import Notifier
from config import Config

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


def _should_fire_report(now_et: datetime, last_report_date: date | None) -> bool:
    """Return True if the EOD/weekly report should fire now.

    Fires during the 16:00:00–16:00:59 ET window, at most once per calendar day.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be timezone-aware (ET)")
    if now_et.hour != 16 or now_et.minute != 0:
        return False
    return last_report_date != now_et.date()


class PositionMonitor:
    def __init__(
        self,
        client: TradingClient,
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
        buys, sells, pnl = await asyncio.to_thread(self._fetch_eod_data)
        await self._notifier.notify_eod_report(buys, sells, pnl)
        self._last_report_date = today
        logger.info("EOD report sent: buys=%d sells=%d pnl=%.2f", buys, sells, pnl)

        if today.weekday() == 4:  # Friday
            w_buys, w_sells, w_pnl = await asyncio.to_thread(self._fetch_weekly_data)
            await self._notifier.notify_weekly_report(w_buys, w_sells, w_pnl)
            logger.info("Weekly report sent: buys=%d sells=%d pnl=%.2f", w_buys, w_sells, w_pnl)

    def _fetch_eod_data(self) -> tuple[int, int, float]:
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        today_start = et.localize(datetime.combine(today, datetime.min.time()))

        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start, limit=500)
        )
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        buys = sum(1 for o in filled if o.side == OrderSide.BUY)
        sells = sum(1 for o in filled if o.side == OrderSide.SELL)

        history = self._client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1D")
        )
        profit_loss = history.profit_loss or []
        pnl = profit_loss[-1] if profit_loss else 0.0

        return buys, sells, pnl

    def _fetch_weekly_data(self) -> tuple[int, int, float]:
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_start_dt = et.localize(datetime.combine(week_start, datetime.min.time()))

        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=week_start_dt, limit=500)
        )
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        buys = sum(1 for o in filled if o.side == OrderSide.BUY)
        sells = sum(1 for o in filled if o.side == OrderSide.SELL)

        history = self._client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1W")
        )
        profit_loss = history.profit_loss or []
        pnl = profit_loss[-1] if profit_loss else 0.0

        return buys, sells, pnl

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
