# trading/order_executor.py
import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
import httpx
from trading.tradier_client import TradierClient
from trading.tradier_client import TradierOrder
from config import Config
from notifications.telegram_notifier import Notifier

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)

_FILL_TIMEOUT = 60.0
_FILL_POLL = 3.0
_PENDING_CLOSE_STATUSES = {"open", "pending", "accepted", "queued", "partially_filled"}


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


class OrderExecutor:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        held_tickers: set[str],
        shorted_tickers: set[str],
        notifier: Notifier,
        db: "TradeDB | None" = None,
    ) -> None:
        self._client = client
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._stop_loss_pct: float = config.stop_loss_pct
        self._take_profit_pct: float = config.take_profit_pct
        self._max_slippage_pct: float = config.max_slippage_pct
        self._extended_move_low_price_pct: float = config.extended_move_low_price_pct
        self._extended_move_any_pct: float = config.extended_move_any_pct
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._db = db
        self._pending_close: set[str] = set()
        # tickers currently inside _wait_for_position — position monitor must skip these
        self._pending_fill: set[str] = set()
        self._trading_paused: bool = False
        # ticker -> (avg_entry_price, qty, trade_id); trade_id is None when db is disabled
        self._position_book: dict[str, tuple[float, int, int | None]] = {}
        # ticker -> OTOCO bracket group order ID (present only for long positions)
        self._bracket_orders: dict[str, str] = {}
        # ticker -> UTC datetime when the hold_hours window expires
        self._hold_until: dict[str, datetime] = {}
        # ticker -> UTC datetime when the position was opened (for age calculation)
        self._hold_opened_at: dict[str, datetime] = {}
        # same-day re-entry guard — reset at midnight
        self._daily_bought_tickers: set[str] = set()
        self._daily_stopped_tickers: set[str] = set()
        # daily P&L counters — reset lazily at start of each new calendar day
        self._last_day: date = date.today()
        self._daily_buys: int = 0
        self._daily_sells: int = 0
        self._daily_realized_pnl: float = 0.0
        # weekly P&L counters — reset lazily at start of each new ISO week (Monday)
        self._last_week_monday: date = _monday_of(date.today())
        self._weekly_buys: int = 0
        self._weekly_sells: int = 0
        self._weekly_realized_pnl: float = 0.0

    @property
    def held_tickers(self) -> frozenset[str]:
        return frozenset(self._held_tickers)

    @property
    def shorted_tickers(self) -> frozenset[str]:
        return frozenset(self._shorted_tickers)

    @property
    def pending_close(self) -> frozenset[str]:
        return frozenset(self._pending_close)

    @property
    def pending_fill(self) -> frozenset[str]:
        return frozenset(self._pending_fill)

    @property
    def trading_paused(self) -> bool:
        return self._trading_paused

    @trading_paused.setter
    def trading_paused(self, value: bool) -> None:
        self._trading_paused = value

    def confirm_closed(self, ticker: str) -> None:
        """Remove the pending-close guard once Tradier no longer returns the position."""
        self._pending_close.discard(ticker)

    def expired_hold_tickers(self) -> frozenset[str]:
        """Return tickers whose hold_hours window has elapsed and are still tracked."""
        now = datetime.now(timezone.utc)
        return frozenset(t for t, exp in self._hold_until.items() if now >= exp)

    @property
    def hold_windows(self) -> dict[str, tuple[datetime, int, datetime]]:
        """Return {ticker: (opened_at, hold_hours, expiry)} for all watched hold windows."""
        result: dict[str, tuple[datetime, int, datetime]] = {}
        for ticker, expiry in self._hold_until.items():
            opened_at = self._hold_opened_at.get(ticker)
            if opened_at is not None:
                hold_hours_val = round((expiry - opened_at).total_seconds() / 3600)
                result[ticker] = (opened_at, hold_hours_val, expiry)
        return result

    async def _wait_for_fill(
        self,
        order_id: str,
        timeout_sec: float = _FILL_TIMEOUT,
        poll_interval: float = _FILL_POLL,
    ) -> tuple[bool, float | None]:
        """Poll until the order is filled, hits a terminal state, or times out.

        Returns (filled, avg_fill_price).
        """
        max_polls = max(1, int(timeout_sec / poll_interval))
        for _ in range(max_polls):
            try:
                status, fill_price = await asyncio.to_thread(self._client.get_order, order_id)
                if status == "filled":
                    return True, fill_price
                if status in ("canceled", "expired", "rejected", "error"):
                    logger.warning("Order %s reached terminal state: %s", order_id, status)
                    return False, None
            except Exception as e:
                logger.warning("Error polling order %s status: %s", order_id, e)
            await asyncio.sleep(poll_interval)
        logger.warning("Order %s fill confirmation timed out after %.0fs", order_id, timeout_sec)
        return False, None

    async def _wait_for_position(
        self,
        ticker: str,
        order_id: str,
        timeout_sec: float = _FILL_TIMEOUT,
        poll_interval: float = _FILL_POLL,
    ) -> tuple[bool, float | None]:
        """Poll until the position appears in the account or the bracket order reaches a terminal state.

        Returns (filled, avg_entry_price). Used for OTOCO entry confirmation because the
        parent OTOCO order status stays 'open' while the bracket is active — polling the
        position list is the reliable signal that leg 0 filled.
        """
        max_polls = max(1, int(timeout_sec / poll_interval))
        for _ in range(max_polls):
            try:
                status, _ = await asyncio.to_thread(self._client.get_order, order_id)
                if status in ("canceled", "expired", "rejected", "error"):
                    logger.warning("OTOCO %s reached terminal state: %s", order_id, status)
                    return False, None
            except Exception as e:
                logger.warning("Error checking OTOCO order %s status: %s", order_id, e)
            try:
                positions = await asyncio.to_thread(self._client.get_all_positions)
                pos = next((p for p in positions if p.symbol == ticker), None)
                if pos is not None and abs(pos.qty) > 0:
                    avg_price = pos.cost_basis / abs(pos.qty) if pos.qty != 0 else None
                    return True, avg_price
            except Exception as e:
                logger.warning("Error polling position for %s: %s", ticker, e)
            await asyncio.sleep(poll_interval)
        logger.warning("OTOCO %s fill confirmation timed out after %.0fs", order_id, timeout_sec)
        return False, None

    def daily_summary(self, target_date: date | None = None) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for today. Resets counters on day boundary."""
        if self._db is not None and hasattr(self._db, "realized_summary_for_et_date"):
            return self._db.realized_summary_for_et_date(target_date or date.today())
        self._maybe_reset_day()
        return self._daily_buys, self._daily_sells, self._daily_realized_pnl

    def weekly_summary(self, week_monday: date | None = None) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for the current ISO week."""
        if self._db is not None and hasattr(self._db, "realized_summary_for_et_week"):
            return self._db.realized_summary_for_et_week(week_monday or _monday_of(date.today()))
        self._maybe_reset_week()
        return self._weekly_buys, self._weekly_sells, self._weekly_realized_pnl

    def _maybe_reset_day(self) -> None:
        today = date.today()
        if today != self._last_day:
            self._daily_buys = 0
            self._daily_sells = 0
            self._daily_realized_pnl = 0.0
            self._daily_bought_tickers.clear()
            self._daily_stopped_tickers.clear()
            self._last_day = today

    def _maybe_reset_week(self) -> None:
        monday = _monday_of(date.today())
        if monday != self._last_week_monday:
            self._weekly_buys = 0
            self._weekly_sells = 0
            self._weekly_realized_pnl = 0.0
            self._last_week_monday = monday

    def _update_close_state(self, ticker: str, pnl_usd: float | None, exit_reason: str = "") -> None:
        """Update in-memory state when a position is closed (success or already-gone error)."""
        self._held_tickers.discard(ticker)
        self._shorted_tickers.discard(ticker)
        self._position_book.pop(ticker, None)
        self._hold_until.pop(ticker, None)
        self._hold_opened_at.pop(ticker, None)
        self._pending_close.add(ticker)
        if exit_reason == "stop_loss":
            self._daily_stopped_tickers.add(ticker)
        self._maybe_reset_day()
        self._maybe_reset_week()
        self._daily_sells += 1
        self._weekly_sells += 1
        if pnl_usd is not None:
            self._daily_realized_pnl += pnl_usd
            self._weekly_realized_pnl += pnl_usd

    def seed_from_db(self, open_trades: list[dict]) -> None:
        """Re-populate position_book, hold_until, and hold_opened_at from DB open trades.

        For tickers still alive at the broker: restore in-memory tracking state.
        For tickers absent from the broker: their bracket fired while the bot was down —
        auto-close the DB record so they don't stay open forever.
        """
        for t in open_trades:
            ticker = t["ticker"]
            trade_id = t.get("id")
            if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
                logger.warning(
                    "DB open trade for %s (trade_id=%s) has no live broker position — "
                    "bracket likely fired offline; recording offline close",
                    ticker, trade_id,
                )
                if self._db is not None and trade_id is not None:
                    try:
                        self._db.record_trade_close(
                            trade_id, None, None, None,
                            "offline_bracket_close",
                            datetime.now(timezone.utc).isoformat(),
                        )
                        logger.info("Recorded offline close for %s trade_id=%s", ticker, trade_id)
                    except Exception as db_err:
                        logger.warning("Failed to record offline close for %s: %s", ticker, db_err)
                continue

            entry_price = float(t.get("entry_price") or 0.0)
            qty = int(t.get("qty") or 0)
            self._position_book[ticker] = (entry_price, qty, trade_id)
            bracket_order_id = t.get("bracket_order_id")
            if bracket_order_id and t.get("side") == "buy":
                self._bracket_orders[ticker] = str(bracket_order_id)

            hold_hours = int(t.get("hold_hours") or 0)
            opened_at: datetime | None = None
            opened_at_str = t.get("opened_at")
            if opened_at_str:
                try:
                    opened_at = datetime.fromisoformat(opened_at_str.replace("Z", "+00:00"))
                    if opened_at.tzinfo is None:
                        opened_at = opened_at.replace(tzinfo=timezone.utc)
                    self._hold_opened_at[ticker] = opened_at
                except (ValueError, TypeError) as e:
                    logger.warning("Could not parse opened_at=%r for %s: %s", opened_at_str, ticker, e)
            if hold_hours > 0:
                if opened_at is not None:
                    self._hold_until[ticker] = opened_at + timedelta(hours=hold_hours)

            logger.info(
                "Seeded position %s: entry=%.4f qty=%d trade_id=%s hold_until=%s",
                ticker, entry_price, qty, trade_id,
                self._hold_until.get(ticker, "none"),
            )

    async def _record_skip_safe(self, decision_id: int | None, reason: str) -> None:
        if self._db is None or decision_id is None:
            return
        try:
            await asyncio.to_thread(self._db.record_skip, decision_id, reason)
        except Exception as db_err:
            logger.warning("Failed to record skip reason for decision %s: %s", decision_id, db_err)

    async def _record_close_safe(
        self,
        trade_id: int | None,
        ticker: str,
        exit_price: float | None,
        pnl_usd: float | None,
        pnl_pct: float | None,
        exit_reason: str,
        closed_at: str | None = None,
    ) -> bool:
        if self._db is None or trade_id is None:
            return False
        try:
            ok = await asyncio.to_thread(
                self._db.record_trade_close,
                trade_id,
                exit_price,
                pnl_usd,
                pnl_pct,
                exit_reason,
                closed_at or datetime.now(timezone.utc).isoformat(),
            )
            if not ok:
                await self._notifier.notify_error(f"close {ticker}", f"analytics DB did not close trade_id={trade_id}")
            return bool(ok)
        except Exception as db_err:
            logger.warning("Failed to record close for %s in analytics DB: %s", ticker, db_err)
            await self._notifier.notify_error(f"close {ticker}", f"analytics DB close failed: {db_err}")
            return False

    @staticmethod
    def _parse_tradier_dt(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None

    async def _find_recent_exit_fill(
        self,
        ticker: str,
        opened_at: datetime | None,
        long_position: bool = True,
    ) -> TradierOrder | None:
        """Return the latest filled broker exit order for ticker after opened_at."""
        exit_sides = {"sell"} if long_position else {"buy_to_cover"}
        try:
            orders = await asyncio.to_thread(self._client.get_account_orders)
        except Exception as e:
            logger.warning("Could not fetch Tradier orders for %s close reconciliation: %s", ticker, e)
            return None

        candidates: list[tuple[datetime, TradierOrder]] = []
        for order in orders:
            if order.symbol != ticker or order.status != "filled" or order.side not in exit_sides:
                continue
            filled_at = self._parse_tradier_dt(order.filled_at)
            if filled_at is None:
                continue
            if opened_at is not None and filled_at < opened_at:
                continue
            candidates.append((filled_at, order))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    async def _find_recent_entry_fill(
        self,
        ticker: str,
        submitted_at: datetime,
        long_position: bool = True,
    ) -> TradierOrder | None:
        """Return the latest filled broker entry order for ticker after submitted_at."""
        entry_sides = {"buy"} if long_position else {"sell_short"}
        try:
            orders = await asyncio.to_thread(self._client.get_account_orders)
        except Exception as e:
            logger.warning("Could not fetch Tradier orders for %s entry reconciliation: %s", ticker, e)
            return None

        candidates: list[tuple[datetime, TradierOrder]] = []
        for order in orders:
            if order.symbol != ticker or order.status != "filled" or order.side not in entry_sides:
                continue
            filled_at = self._parse_tradier_dt(order.filled_at)
            if filled_at is None:
                continue
            # Allow a small clock/order propagation cushion around submission time.
            if filled_at < submitted_at - timedelta(minutes=2):
                continue
            candidates.append((filled_at, order))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    async def _find_pending_market_close_order(self, ticker: str) -> TradierOrder | None:
        """Return an existing pending market close order so we don't double-submit exits."""
        close_side = "buy_to_cover" if ticker in self._shorted_tickers else "sell"
        try:
            orders = await asyncio.to_thread(self._client.get_account_orders)
        except Exception as e:
            logger.warning("Could not fetch Tradier orders before closing %s: %s", ticker, e)
            return None
        for order in orders:
            if (
                order.symbol == ticker
                and order.side == close_side
                and order.status in _PENDING_CLOSE_STATUSES
                and order.order_type == "market"
            ):
                return order
        return None

    async def _reconcile_fast_bracket_round_trip(
        self,
        ticker: str,
        decision_id: int | None,
        submitted_at: datetime,
        fallback_qty: int,
        fill_latency_sec: float | None,
        hold_hours: int,
    ) -> bool:
        """Handle buy+bracket-exit sequences that complete between position polls."""
        entry_fill = await self._find_recent_entry_fill(ticker, submitted_at, long_position=True)
        if entry_fill is None or entry_fill.avg_fill_price is None:
            return False
        entry_time = self._parse_tradier_dt(entry_fill.filled_at)
        exit_fill = await self._find_recent_exit_fill(ticker, entry_time, long_position=True)
        if exit_fill is None or exit_fill.avg_fill_price is None:
            return False

        qty = int(abs(entry_fill.quantity or fallback_qty))
        entry_price = entry_fill.avg_fill_price
        exit_price = exit_fill.avg_fill_price
        exit_reason = "stop_loss" if exit_fill.order_type == "stop" else (
            "take_profit" if exit_fill.order_type == "limit" else "bracket_order"
        )
        pnl_usd = (exit_price - entry_price) * qty
        pnl_pct = (exit_price - entry_price) / entry_price
        trade_id: int | None = None

        if self._db is not None:
            try:
                opened_at = entry_fill.filled_at or datetime.now(timezone.utc).isoformat()
                trade_id = await asyncio.to_thread(
                        self._db.record_trade_open,
                        decision_id,
                        ticker,
                    "buy",
                    qty,
                    entry_price,
                        opened_at,
                        fill_latency_sec,
                        hold_hours,
                        "reconciled",
                    )
                await self._record_close_safe(
                    trade_id,
                    ticker,
                    exit_price,
                    pnl_usd,
                    pnl_pct,
                    exit_reason,
                    exit_fill.filled_at,
                )
            except Exception as db_err:
                logger.warning("Failed to record fast bracket round trip for %s: %s", ticker, db_err)

        self._position_book[ticker] = (entry_price, qty, trade_id)
        self._pending_fill.discard(ticker)
        self._bracket_orders.pop(ticker, None)
        self._update_close_state(ticker, pnl_usd, exit_reason)

        logger.info(
            "FAST BRACKET ROUND TRIP for %s entry=$%.2f exit=$%.2f qty=%d pnl=%.2f reason=%s",
            ticker, entry_price, exit_price, qty, pnl_usd, exit_reason,
        )
        await self._notifier.notify_buy(
            ticker,
            entry_price * qty,
            "reconciled",
            fill_price=entry_price,
            fill_latency_sec=fill_latency_sec,
        )
        await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
        return True

    async def handle_bracket_close(self, ticker: str, current_price: float | None) -> None:
        """Called by PositionMonitor when a long position disappears after an OTOCO bracket fired."""
        entry_price, qty_held, trade_id = self._position_book.get(ticker, (0.0, 0, None))
        opened_at = self._hold_opened_at.get(ticker)
        self._bracket_orders.pop(ticker, None)

        pnl_pct: float | None = None
        pnl_usd: float | None = None
        exit_price: float | None = current_price
        exit_reason = "bracket_order"
        closed_at: str | None = None

        exit_fill = await self._find_recent_exit_fill(ticker, opened_at, long_position=True)
        if exit_fill and exit_fill.avg_fill_price is not None:
            exit_price = exit_fill.avg_fill_price
            closed_at = exit_fill.filled_at
            if exit_fill.order_type == "stop":
                exit_reason = "stop_loss"
            elif exit_fill.order_type == "limit":
                exit_reason = "take_profit"
            if entry_price > 0 and qty_held > 0:
                pnl_pct = (exit_price - entry_price) / entry_price
                pnl_usd = (exit_price - entry_price) * qty_held

        if pnl_usd is None and current_price and entry_price > 0 and qty_held > 0:
            # Infer which bracket leg fired from price direction, then use the known
            # TP/SL levels for P&L — the live quote arrives up to ~30s after the fill
            # and does not reflect the actual bracket execution price.
            tp_price = round(entry_price * (1 + self._take_profit_pct), 2)
            sl_price = round(entry_price * (1 - self._stop_loss_pct), 2)
            if current_price >= entry_price:
                exit_price = tp_price
                exit_reason = "take_profit"
            else:
                exit_price = sl_price
                exit_reason = "stop_loss"
            pnl_pct = (exit_price - entry_price) / entry_price
            pnl_usd = (exit_price - entry_price) * qty_held

        self._update_close_state(ticker, pnl_usd, exit_reason)
        await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at)

        pnl_str = f"{pnl_pct * 100:.2f}%" if pnl_pct is not None else "unknown"
        logger.info("BRACKET CLOSE for %s price=$%.2f pnl=%s reason=%s",
                    ticker, current_price or 0, pnl_str, exit_reason)
        await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)

    async def buy(self, ticker: str, decision_id: int | None = None, decision_monotonic: float | None = None, hold_hours: int = 0) -> None:
        if self._trading_paused:
            logger.info("Trading paused — skipping buy for %s", ticker)
            await self._record_skip_safe(decision_id, "trading_paused")
            return
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            await self._record_skip_safe(decision_id, "already_held")
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            await self._record_skip_safe(decision_id, "already_shorted")
            return
        if ticker in self._daily_stopped_tickers:
            logger.info("SKIP [same_day_reentry_block] %s — stopped out earlier today", ticker)
            await self._record_skip_safe(decision_id, "same_day_reentry_block")
            return
        if ticker in self._daily_bought_tickers:
            logger.info("SKIP [same_day_reentry_block] %s — already bought and closed today", ticker)
            await self._record_skip_safe(decision_id, "same_day_reentry_block")
            return
        try:
            buying_power = await asyncio.to_thread(self._client.get_buying_power)
            if buying_power < self._notional_usd:
                logger.info(
                    "Skipping buy for %s — insufficient funds (have $%.2f, need $%.2f)",
                    ticker, buying_power, self._notional_usd,
                )
                await self._record_skip_safe(decision_id, "insufficient_funds")
                return
            quotes_ext = await asyncio.to_thread(self._client.get_quotes_with_open, [ticker])
            quote = quotes_ext.get(ticker)
            if not quote:
                logger.error("No quote available for %s — skipping buy", ticker)
                await self._record_skip_safe(decision_id, "no_quote")
                return
            price, open_price = quote

            # --- intraday extension filter ---
            if open_price is not None and open_price > 0:
                intraday_move = (price - open_price) / open_price
                if price < 5.0 and intraday_move > self._extended_move_low_price_pct:
                    logger.info(
                        "SKIP [extended_move_block] %s — price $%.2f is up %.1f%% from open $%.2f (low-price threshold %.0f%%)",
                        ticker, price, intraday_move * 100, open_price, self._extended_move_low_price_pct * 100,
                    )
                    await self._record_skip_safe(decision_id, "extended_move_block")
                    return
                if intraday_move > self._extended_move_any_pct:
                    logger.info(
                        "SKIP [extended_move_block] %s — price $%.2f is up %.1f%% from open $%.2f (any-price threshold %.0f%%)",
                        ticker, price, intraday_move * 100, open_price, self._extended_move_any_pct * 100,
                    )
                    await self._record_skip_safe(decision_id, "extended_move_block")
                    return

                # --- falling on good news filter ---
                if intraday_move < -0.03:
                    logger.info(
                        "SKIP [negative_price_confirmation_block] %s — price $%.2f is down %.1f%% from session open $%.2f despite positive catalyst",
                        ticker, price, intraday_move * 100, open_price,
                    )
                    await self._record_skip_safe(decision_id, "negative_price_confirmation_block")
                    return

            qty = math.floor(self._notional_usd / price)
            if qty == 0:
                logger.info(
                    "Skipping buy for %s — price $%.2f exceeds budget $%.2f",
                    ticker, price, self._notional_usd,
                )
                await self._record_skip_safe(decision_id, "budget_exceeded")
                return

            # --- limit entry for volatile / low-price stocks ---
            entry_limit: float | None = None
            if open_price is not None and open_price > 0:
                intraday_move_pct = (price - open_price) / open_price
                if price < 5.0 or intraday_move_pct > 0.10:
                    entry_limit = price * (1 + self._max_slippage_pct)
            else:
                if price < 5.0:
                    entry_limit = price * (1 + self._max_slippage_pct)

            tp_price = round(price * (1 + self._take_profit_pct), 2)
            sl_price = round(price * (1 - self._stop_loss_pct), 2)

            if entry_limit is not None:
                logger.info(
                    "OTOCO LIMIT entry for %s @ $%.4f (quote $%.2f + %.0f%% slippage), TP=$%.2f SL=$%.2f",
                    ticker, entry_limit, price, self._max_slippage_pct * 100, tp_price, sl_price,
                )

            order_id = await asyncio.to_thread(
                self._client.submit_otoco_order, ticker, qty, tp_price, sl_price, entry_limit
            )
            submitted_at_utc = datetime.now(timezone.utc)
            _submitted_at = time.monotonic()
            self._bracket_orders[ticker] = order_id

            # Guard against duplicate buys immediately — broker accepted the order
            self._held_tickers.add(ticker)
            self._daily_bought_tickers.add(ticker)
            self._position_book[ticker] = (price, qty, None)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1

            # Wait for entry leg fill: poll positions (OTOCO parent stays 'open' while bracket is active).
            # Guard pending_fill so the position monitor does not mistake a not-yet-filled entry as a
            # bracket close (the race that caused false handle_bracket_close calls for KYIV/AMC/ZTEK).
            self._pending_fill.add(ticker)
            filled, fill_price = await self._wait_for_position(ticker, order_id)
            # Measure latency from decision time when provided, otherwise from submission
            if decision_monotonic is not None:
                fill_latency_sec = time.monotonic() - decision_monotonic
            else:
                fill_latency_sec = time.monotonic() - _submitted_at
            if not filled:
                reconciled = await self._reconcile_fast_bracket_round_trip(
                    ticker,
                    decision_id,
                    submitted_at_utc,
                    qty,
                    fill_latency_sec,
                    hold_hours,
                )
                if reconciled:
                    return
                # Synchronously clean up all tracked state before any awaits so the position monitor
                # cannot observe a partially-rolled-back entry during cancel_order.
                self._held_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._bracket_orders.pop(ticker, None)
                if ticker not in self._pending_close:
                    # handle_bracket_close hasn't fired concurrently — safe to roll back day guards.
                    self._daily_bought_tickers.discard(ticker)
                    self._daily_buys -= 1
                    self._weekly_buys -= 1
                self._pending_fill.discard(ticker)
                try:
                    await asyncio.to_thread(self._client.cancel_order, order_id)
                except Exception:
                    pass
                logger.warning("OTOCO %s for %s fill unconfirmed — rolling back state", order_id, ticker)
                await self._notifier.notify_error(f"buy {ticker}", f"order {order_id} fill unconfirmed")
                return
            actual_price = fill_price if fill_price else price
            self._position_book[ticker] = (actual_price, qty, None)

            opened_at = datetime.now(timezone.utc).isoformat()
            opened_at_utc = datetime.fromisoformat(opened_at)
            self._hold_opened_at[ticker] = opened_at_utc
            if hold_hours > 0:
                self._hold_until[ticker] = opened_at_utc + timedelta(hours=hold_hours)

            if self._db is not None:
                try:
                    trade_id = await asyncio.to_thread(
                        self._db.record_trade_open,
                        decision_id, ticker, "buy", qty, actual_price, opened_at,
                        fill_latency_sec, hold_hours, order_id,
                    )
                    self._position_book[ticker] = (actual_price, qty, trade_id)
                    logger.info(
                        "TRADE INSERTED: trade_id=%s decision_id=%s ticker=%s",
                        trade_id, decision_id, ticker,
                    )
                except Exception as db_err:
                    logger.warning("Failed to record buy for %s in analytics DB: %s", ticker, db_err)
                    await self._notifier.notify_error(f"buy {ticker}", f"analytics DB open failed: {db_err}")

            # Clear pending_fill only after position_book has the final trade_id.
            # Holding it through the DB insert prevents handle_bracket_close from
            # firing with trade_id=None if the bracket executes during that await (BLSH pattern).
            self._pending_fill.discard(ticker)

            logger.info("BUY filled for %s qty=%d @ $%.2f in %.1fs — order %s", ticker, qty, actual_price, fill_latency_sec, order_id)
            await self._notifier.notify_buy(ticker, actual_price * qty, order_id, fill_price=actual_price, fill_latency_sec=fill_latency_sec)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._record_skip_safe(decision_id, "buy_exception")
            await self._notifier.notify_error(f"buy {ticker}", str(e))

    async def short(self, ticker: str, decision_id: int | None = None, decision_monotonic: float | None = None, hold_hours: int = 0) -> None:
        if self._trading_paused:
            logger.info("Trading paused — skipping short for %s", ticker)
            await self._record_skip_safe(decision_id, "trading_paused")
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping short for %s — already shorted", ticker)
            await self._record_skip_safe(decision_id, "already_shorted")
            return
        if ticker in self._held_tickers:
            logger.info("Skipping short for %s — currently held long, sell first", ticker)
            await self._record_skip_safe(decision_id, "already_held")
            return
        try:
            order_id = await asyncio.to_thread(
                self._client.submit_order, ticker, "sell_short", self._short_qty
            )
            _submitted_at = time.monotonic()

            # Guard immediately — broker accepted the order
            self._shorted_tickers.add(ticker)
            self._position_book[ticker] = (0.0, self._short_qty, None)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1

            # Wait for fill confirmation before recording or notifying
            filled, fill_price = await self._wait_for_fill(order_id)
            # Measure latency from decision time when provided, otherwise from submission
            if decision_monotonic is not None:
                fill_latency_sec = time.monotonic() - decision_monotonic
            else:
                fill_latency_sec = time.monotonic() - _submitted_at
            if not filled:
                logger.warning("SHORT order %s for %s fill unconfirmed — rolling back state", order_id, ticker)
                self._shorted_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._daily_buys -= 1
                self._weekly_buys -= 1
                await self._notifier.notify_error(f"short {ticker}", f"order {order_id} fill unconfirmed")
                return

            actual_price = fill_price or 0.0
            self._position_book[ticker] = (actual_price, self._short_qty, None)

            opened_at = datetime.now(timezone.utc).isoformat()
            opened_at_utc = datetime.fromisoformat(opened_at)
            self._hold_opened_at[ticker] = opened_at_utc
            if hold_hours > 0:
                self._hold_until[ticker] = opened_at_utc + timedelta(hours=hold_hours)

            if self._db is not None:
                try:
                    trade_id = await asyncio.to_thread(
                        self._db.record_trade_open,
                        decision_id, ticker, "short", self._short_qty, actual_price or None, opened_at, fill_latency_sec, hold_hours,
                    )
                    self._position_book[ticker] = (actual_price, self._short_qty, trade_id)
                except Exception as db_err:
                    logger.warning("Failed to record short for %s in analytics DB: %s", ticker, db_err)
                    await self._notifier.notify_error(f"short {ticker}", f"analytics DB open failed: {db_err}")

            logger.info("SHORT filled for %s qty=%d @ $%.2f in %.1fs — order %s", ticker, self._short_qty, actual_price, fill_latency_sec, order_id)
            await self._notifier.notify_short(ticker, self._short_qty, order_id, fill_price=fill_price, fill_latency_sec=fill_latency_sec)
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
            await self._record_skip_safe(decision_id, "short_exception")
            await self._notifier.notify_error(f"short {ticker}", str(e))

    async def sell(
        self,
        ticker: str,
        pnl_pct: float | None = None,
        pnl_usd: float | None = None,
        exit_reason: str = "llm",
    ) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return

        # Cancel any active OTOCO bracket before closing to avoid order conflicts
        bracket_id = self._bracket_orders.pop(ticker, None)
        if bracket_id:
            try:
                await asyncio.to_thread(self._client.cancel_order, bracket_id)
                logger.info("Cancelled bracket order %s for %s before manual close", bracket_id, ticker)
            except Exception as e:
                logger.warning("Failed to cancel bracket %s for %s: %s", bracket_id, ticker, e)

        existing_close = await self._find_pending_market_close_order(ticker)
        if existing_close is not None:
            self._pending_close.add(ticker)
            logger.warning(
                "Close already pending for %s via order %s — not submitting duplicate",
                ticker, existing_close.order_id or "unknown",
            )
            await self._notifier.notify_error(
                f"sell {ticker}",
                f"close already pending via order {existing_close.order_id or 'unknown'}",
            )
            return

        entry_price, qty_held, trade_id = self._position_book.get(ticker, (0.0, 0, None))
        exit_price: float | None = None
        pnl_was_computed = False

        try:
            # Estimate P&L for long positions when not provided by caller
            if pnl_usd is None and ticker in self._held_tickers and entry_price > 0 and qty_held > 0:
                quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
                current = quotes.get(ticker, 0.0)
                if current:
                    exit_price = current
                    pnl_usd = (current - entry_price) * qty_held
                    pnl_pct = (current - entry_price) / entry_price
                    pnl_was_computed = True

            order_id = await asyncio.to_thread(self._client.close_position, ticker)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
                self._update_close_state(ticker, pnl_usd, exit_reason)
                await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason)
                logger.warning(
                    "Close %s — position already gone or closing (HTTP %s), removing from tracking",
                    ticker, e.response.status_code,
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
            return
        except ValueError as e:
            if "no open position" in str(e).lower():
                self._update_close_state(ticker, pnl_usd, exit_reason)
                await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason)
                logger.warning("Close %s — position not found in broker, removing from tracking", ticker)
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
            return
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
            return

        # Order submitted; do not mark analytics closed until the broker confirms a fill.
        self._pending_close.add(ticker)
        if exit_reason == "hold_hours":
            logger.info("TIMED EXIT SELL SUBMITTED: ticker=%s order_id=%s trade_id=%s", ticker, order_id, trade_id)

        filled, fill_price = await self._wait_for_fill(order_id)
        if not filled:
            logger.warning("SELL order %s for %s fill unconfirmed — skipping sell notification", order_id, ticker)
            self._pending_close.discard(ticker)
            await self._notifier.notify_error(f"sell {ticker}", f"order {order_id} fill unconfirmed")
            return

        # Refine P&L with actual fill price when available
        if fill_price and pnl_was_computed and entry_price > 0 and qty_held > 0:
            actual_pnl_usd = (fill_price - entry_price) * qty_held
            pnl_delta = actual_pnl_usd - (pnl_usd or 0.0)
            self._daily_realized_pnl += pnl_delta
            self._weekly_realized_pnl += pnl_delta
            pnl_usd = actual_pnl_usd
            pnl_pct = (fill_price - entry_price) / entry_price
            exit_price = fill_price

        self._update_close_state(ticker, pnl_usd, exit_reason)
        await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason)
        if exit_reason == "hold_hours":
            logger.info("TIMED EXIT CLOSED TRADE: ticker=%s trade_id=%s exit_price=%s pnl_pct=%s",
                        ticker, trade_id, exit_price, f"{pnl_pct * 100:.2f}%" if pnl_pct is not None else "unknown")
        logger.info("CLOSED position for %s", ticker)
        await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
