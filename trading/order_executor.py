import logging
from datetime import date
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from config import Config
from notifications.telegram_notifier import Notifier

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, config: Config, held_tickers: set[str], shorted_tickers: set[str], notifier: Notifier) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._open_dates: dict[str, date] = {}

    @property
    def held_tickers(self) -> frozenset[str]:
        return frozenset(self._held_tickers)

    @property
    def shorted_tickers(self) -> frozenset[str]:
        return frozenset(self._shorted_tickers)

    def is_opened_today(self, ticker: str) -> bool:
        return self._open_dates.get(ticker) == date.today()

    async def buy(self, ticker: str) -> None:
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
            logger.info(
                "BUY order accepted for %s $%.2f — order %s (pending fill)",
                ticker, self._notional_usd, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_buy(ticker, self._notional_usd, str(getattr(order, "id", "unknown")))
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._notifier.notify_error(f"buy {ticker}", str(e))

    async def short(self, ticker: str) -> None:
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
            logger.info(
                "SHORT order accepted for %s qty=%d — order %s (pending fill)",
                ticker, self._short_qty, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_short(ticker, self._short_qty, str(getattr(order, "id", "unknown")))
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
            await self._notifier.notify_error(f"short {ticker}", str(e))

    async def sell(self, ticker: str) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            self._shorted_tickers.discard(ticker)
            self._open_dates.pop(ticker, None)
            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status in (404, 422):
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                self._open_dates.pop(ticker, None)
                logger.warning("Close %s — position not found (status %s), removing from tracking", ticker, status)
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
