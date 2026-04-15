# trading/order_executor.py
import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
import httpx
from trading.tradier_client import TradierClient
from config import Config
from notifications.telegram_notifier import Notifier

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


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

    def confirm_closed(self, ticker: str) -> None:
        """Remove the pending-close guard once Tradier no longer returns the position."""
        self._pending_close.discard(ticker)

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

    async def buy(self, ticker: str, decision_id: int | None = None) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            return
        try:
            quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
            price = quotes.get(ticker)
            if not price:
                logger.error("No quote available for %s — skipping buy", ticker)
                return
            qty = max(1, math.floor(self._notional_usd / price))
            order_id = await asyncio.to_thread(self._client.submit_order, ticker, "buy", qty)

            trade_id: int | None = None
            if self._db is not None:
                opened_at = datetime.now(timezone.utc).isoformat()
                trade_id = await asyncio.to_thread(
                    self._db.record_trade_open, decision_id, ticker, "buy", qty, price, opened_at
                )

            self._held_tickers.add(ticker)
            self._position_book[ticker] = (price, qty, trade_id)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1
            logger.info(
                "BUY order accepted for %s qty=%d @ $%.2f — order %s (pending fill)",
                ticker, qty, price, order_id,
            )
            await self._notifier.notify_buy(ticker, self._notional_usd, order_id)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._notifier.notify_error(f"buy {ticker}", str(e))

    async def short(self, ticker: str, decision_id: int | None = None) -> None:
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

            trade_id = None
            if self._db is not None:
                opened_at = datetime.now(timezone.utc).isoformat()
                trade_id = await asyncio.to_thread(
                    self._db.record_trade_open,
                    decision_id, ticker, "short", self._short_qty, None, opened_at,
                )

            self._shorted_tickers.add(ticker)
            self._position_book[ticker] = (0.0, self._short_qty, trade_id)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1
            logger.info(
                "SHORT order accepted for %s qty=%d — order %s (pending fill)",
                ticker, self._short_qty, order_id,
            )
            await self._notifier.notify_short(ticker, self._short_qty, order_id)
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

        # Extract trade_id before modifying _position_book
        trade_id: int | None = None
        if ticker in self._position_book:
            _, _, trade_id = self._position_book[ticker]

        exit_price: float | None = None
        try:
            # Compute realized P&L for long positions when not already provided by caller
            if pnl_usd is None and ticker in self._held_tickers and ticker in self._position_book:
                entry_price, qty, _ = self._position_book[ticker]
                if entry_price > 0:
                    quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
                    current = quotes.get(ticker, 0.0)
                    if current:
                        exit_price = current
                        pnl_usd = (current - entry_price) * qty
                        pnl_pct = (current - entry_price) / entry_price

            await asyncio.to_thread(self._client.close_position, ticker)
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

            if self._db is not None and trade_id is not None:
                closed_at = datetime.now(timezone.utc).isoformat()
                await asyncio.to_thread(
                    self._db.record_trade_close,
                    trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                )

            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
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
                if self._db is not None and trade_id is not None:
                    closed_at = datetime.now(timezone.utc).isoformat()
                    await asyncio.to_thread(
                        self._db.record_trade_close,
                        trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                    )
                logger.warning(
                    "Close %s — position already gone or closing (HTTP %s), removing from tracking",
                    ticker, e.response.status_code,
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except ValueError as e:
            if "no open position" in str(e).lower():
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
                if self._db is not None and trade_id is not None:
                    closed_at = datetime.now(timezone.utc).isoformat()
                    await asyncio.to_thread(
                        self._db.record_trade_close,
                        trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                    )
                logger.warning(
                    "Close %s — position not found in broker, removing from tracking", ticker
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
