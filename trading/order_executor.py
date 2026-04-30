# trading/order_executor.py
import asyncio
import logging
import math
import time
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
import httpx
from trading.tradier_client import TradierClient
from config import Config
from notifications.telegram_notifier import Notifier

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)

_FILL_TIMEOUT = 60.0
_FILL_POLL = 3.0


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
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._db = db
        self._pending_close: set[str] = set()
        self._trading_paused: bool = False
        # ticker -> (avg_entry_price, qty, trade_id); trade_id is None when db is disabled
        self._position_book: dict[str, tuple[float, int, int | None]] = {}
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
    def trading_paused(self) -> bool:
        return self._trading_paused

    @trading_paused.setter
    def trading_paused(self, value: bool) -> None:
        self._trading_paused = value

    def confirm_closed(self, ticker: str) -> None:
        """Remove the pending-close guard once Tradier no longer returns the position."""
        self._pending_close.discard(ticker)

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

    def daily_summary(self) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for today. Resets counters on day boundary."""
        self._maybe_reset_day()
        return self._daily_buys, self._daily_sells, self._daily_realized_pnl

    def weekly_summary(self) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for the current ISO week."""
        self._maybe_reset_week()
        return self._weekly_buys, self._weekly_sells, self._weekly_realized_pnl

    def _maybe_reset_day(self) -> None:
        today = date.today()
        if today != self._last_day:
            self._daily_buys = 0
            self._daily_sells = 0
            self._daily_realized_pnl = 0.0
            self._last_day = today

    def _maybe_reset_week(self) -> None:
        monday = _monday_of(date.today())
        if monday != self._last_week_monday:
            self._weekly_buys = 0
            self._weekly_sells = 0
            self._weekly_realized_pnl = 0.0
            self._last_week_monday = monday

    def _update_close_state(self, ticker: str, pnl_usd: float | None) -> None:
        """Update in-memory state when a position is closed (success or already-gone error)."""
        self._held_tickers.discard(ticker)
        self._shorted_tickers.discard(ticker)
        self._position_book.pop(ticker, None)
        self._pending_close.add(ticker)
        self._maybe_reset_day()
        self._maybe_reset_week()
        self._daily_sells += 1
        self._weekly_sells += 1
        if pnl_usd is not None:
            self._daily_realized_pnl += pnl_usd
            self._weekly_realized_pnl += pnl_usd

    async def _record_close_safe(
        self,
        trade_id: int | None,
        ticker: str,
        exit_price: float | None,
        pnl_usd: float | None,
        pnl_pct: float | None,
        exit_reason: str,
    ) -> None:
        if self._db is None or trade_id is None:
            return
        try:
            closed_at = datetime.now(timezone.utc).isoformat()
            await asyncio.to_thread(
                self._db.record_trade_close,
                trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
            )
        except Exception as db_err:
            logger.warning("Failed to record close for %s in analytics DB: %s", ticker, db_err)

    async def buy(self, ticker: str, decision_id: int | None = None, decision_monotonic: float | None = None) -> None:
        if self._trading_paused:
            logger.info("Trading paused — skipping buy for %s", ticker)
            return
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            return
        try:
            buying_power = await asyncio.to_thread(self._client.get_buying_power)
            if buying_power < self._notional_usd:
                logger.info(
                    "Skipping buy for %s — insufficient funds (have $%.2f, need $%.2f)",
                    ticker, buying_power, self._notional_usd,
                )
                return
            quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
            price = quotes.get(ticker)
            if not price:
                logger.error("No quote available for %s — skipping buy", ticker)
                return
            qty = math.floor(self._notional_usd / price)
            if qty == 0:
                logger.info(
                    "Skipping buy for %s — price $%.2f exceeds budget $%.2f",
                    ticker, price, self._notional_usd,
                )
                return
            order_id = await asyncio.to_thread(self._client.submit_order, ticker, "buy", qty)
            _submitted_at = time.monotonic()

            # Guard against duplicate buys immediately — broker accepted the order
            self._held_tickers.add(ticker)
            self._position_book[ticker] = (price, qty, None)
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
                logger.warning("BUY order %s for %s fill unconfirmed — rolling back state", order_id, ticker)
                self._held_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._daily_buys -= 1
                self._weekly_buys -= 1
                await self._notifier.notify_error(f"buy {ticker}", f"order {order_id} fill unconfirmed")
                return

            actual_price = fill_price if fill_price else price
            self._position_book[ticker] = (actual_price, qty, None)

            if self._db is not None:
                try:
                    opened_at = datetime.now(timezone.utc).isoformat()
                    trade_id = await asyncio.to_thread(
                        self._db.record_trade_open, decision_id, ticker, "buy", qty, actual_price, opened_at, fill_latency_sec
                    )
                    self._position_book[ticker] = (actual_price, qty, trade_id)
                except Exception as db_err:
                    logger.warning("Failed to record buy for %s in analytics DB: %s", ticker, db_err)

            logger.info("BUY filled for %s qty=%d @ $%.2f in %.1fs — order %s", ticker, qty, actual_price, fill_latency_sec, order_id)
            await self._notifier.notify_buy(ticker, actual_price * qty, order_id, fill_price=actual_price, fill_latency_sec=fill_latency_sec)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._notifier.notify_error(f"buy {ticker}", str(e))

    async def short(self, ticker: str, decision_id: int | None = None, decision_monotonic: float | None = None) -> None:
        if self._trading_paused:
            logger.info("Trading paused — skipping short for %s", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping short for %s — already shorted", ticker)
            return
        if ticker in self._held_tickers:
            logger.info("Skipping short for %s — currently held long, sell first", ticker)
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

            if self._db is not None:
                try:
                    opened_at = datetime.now(timezone.utc).isoformat()
                    trade_id = await asyncio.to_thread(
                        self._db.record_trade_open,
                        decision_id, ticker, "short", self._short_qty, actual_price or None, opened_at, fill_latency_sec,
                    )
                    self._position_book[ticker] = (actual_price, self._short_qty, trade_id)
                except Exception as db_err:
                    logger.warning("Failed to record short for %s in analytics DB: %s", ticker, db_err)

            logger.info("SHORT filled for %s qty=%d @ $%.2f in %.1fs — order %s", ticker, self._short_qty, actual_price, fill_latency_sec, order_id)
            await self._notifier.notify_short(ticker, self._short_qty, order_id, fill_price=fill_price, fill_latency_sec=fill_latency_sec)
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
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
                self._update_close_state(ticker, pnl_usd)
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
                self._update_close_state(ticker, pnl_usd)
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

        # Order submitted — update state immediately to prevent re-sell attempts
        self._update_close_state(ticker, pnl_usd)

        filled, fill_price = await self._wait_for_fill(order_id)
        if not filled:
            logger.warning("SELL order %s for %s fill unconfirmed — skipping sell notification", order_id, ticker)
            await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason)
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

        await self._record_close_safe(trade_id, ticker, exit_price, pnl_usd, pnl_pct, exit_reason)
        logger.info("CLOSED position for %s", ticker)
        await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
