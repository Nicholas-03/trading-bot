import logging
from datetime import date, datetime, timezone
from typing import TYPE_CHECKING
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from config import Config
from notifications.telegram_notifier import Notifier

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(
        self,
        client: TradingClient,
        config: Config,
        held_tickers: set[str],
        shorted_tickers: set[str],
        notifier: Notifier,
        open_dates: dict[str, date] | None = None,
        db: "TradeDB | None" = None,
    ) -> None:
        self._client = client
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._open_dates: dict[str, date] = dict(open_dates) if open_dates else {}
        self._pending_close: set[str] = set()
        self._db = db
        self._trade_ids: dict[str, int] = {}

    @property
    def held_tickers(self) -> frozenset[str]:
        return frozenset(self._held_tickers)

    @property
    def shorted_tickers(self) -> frozenset[str]:
        return frozenset(self._shorted_tickers)

    def is_opened_today(self, ticker: str) -> bool:
        return self._open_dates.get(ticker) == date.today()

    @property
    def pending_close(self) -> frozenset[str]:
        return frozenset(self._pending_close)

    def confirm_closed(self, ticker: str) -> None:
        """Call once Alpaca no longer returns the position, to remove the pending-close guard."""
        self._pending_close.discard(ticker)

    def _order_qty(self, order, fallback: int = 0) -> int:
        for attr in ("filled_qty", "qty"):
            raw = getattr(order, attr, None)
            if raw not in (None, ""):
                try:
                    return int(float(raw))
                except (TypeError, ValueError):
                    pass
        return fallback

    def _order_price(self, order) -> float | None:
        for attr in ("filled_avg_price", "avg_fill_price", "limit_price"):
            raw = getattr(order, attr, None)
            if raw not in (None, ""):
                try:
                    return float(raw)
                except (TypeError, ValueError):
                    pass
        return None

    def _record_open_trade_safe(self, decision_id: int | None, ticker: str, side: str, order, fallback_qty: int = 0) -> None:
        if self._db is None:
            return
        try:
            trade_id = self._db.record_trade_open(
                decision_id=decision_id,
                ticker=ticker,
                side=side,
                qty=self._order_qty(order, fallback=fallback_qty),
                entry_price=self._order_price(order),
                opened_at=datetime.now(timezone.utc).isoformat(),
            )
            self._trade_ids[ticker] = trade_id
        except Exception as exc:
            logger.warning("Analytics trade open record failed for %s: %s", ticker, exc)

    def _find_open_trade_id(self, ticker: str) -> int | None:
        trade_id = self._trade_ids.get(ticker)
        if trade_id is not None:
            return trade_id
        if self._db is None:
            return None
        try:
            for row in reversed(self._db.get_open_trades()):
                if row.get("ticker") == ticker:
                    found = int(row["id"])
                    self._trade_ids[ticker] = found
                    return found
        except Exception as exc:
            logger.warning("Analytics open-trade lookup failed for %s: %s", ticker, exc)
        return None

    def _record_trade_close_safe(
        self,
        ticker: str,
        pnl_pct: float | None,
        pnl_usd: float | None,
        exit_reason: str,
    ) -> None:
        if self._db is None:
            return
        trade_id = self._find_open_trade_id(ticker)
        if trade_id is None:
            logger.warning("Analytics has no open trade row for close: %s", ticker)
            return
        try:
            self._db.record_trade_close(
                trade_id=trade_id,
                exit_price=None,
                pnl_usd=pnl_usd,
                pnl_pct=pnl_pct,
                exit_reason=exit_reason,
                closed_at=datetime.now(timezone.utc).isoformat(),
            )
            self._trade_ids.pop(ticker, None)
        except Exception as exc:
            logger.warning("Analytics trade close record failed for %s: %s", ticker, exc)

    async def buy(self, ticker: str, decision_id: int | None = None) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            return
        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    notional=self._notional_usd,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._held_tickers.add(ticker)
            self._open_dates[ticker] = date.today()
            self._record_open_trade_safe(decision_id, ticker, "buy", order)
            logger.info(
                "BUY order accepted for %s $%.2f — order %s (pending fill)",
                ticker, self._notional_usd, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_buy(ticker, self._notional_usd, str(getattr(order, "id", "unknown")))
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
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    qty=self._short_qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._shorted_tickers.add(ticker)
            self._open_dates[ticker] = date.today()
            self._record_open_trade_safe(decision_id, ticker, "short", order, fallback_qty=self._short_qty)
            logger.info(
                "SHORT order accepted for %s qty=%d — order %s (pending fill)",
                ticker, self._short_qty, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_short(ticker, self._short_qty, str(getattr(order, "id", "unknown")))
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
            await self._notifier.notify_error(f"short {ticker}", str(e))

    async def sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            self._shorted_tickers.discard(ticker)
            self._open_dates.pop(ticker, None)
            self._pending_close.add(ticker)
            self._record_trade_close_safe(ticker, pnl_pct, pnl_usd, "closed")
            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
        except APIError as e:
            status = getattr(e, "status_code", None)
            err_str = str(e)
            # 404/422: position gone; 40310000: qty held_for_orders (close already pending)
            if status in (404, 422) or "held_for_orders" in err_str:
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                self._open_dates.pop(ticker, None)
                self._pending_close.add(ticker)
                self._record_trade_close_safe(ticker, pnl_pct, pnl_usd, "already_closing_or_gone")
                logger.warning(
                    "Close %s — position already closing or gone (status %s), removing from tracking",
                    ticker, status,
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
