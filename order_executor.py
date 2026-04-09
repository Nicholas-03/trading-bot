import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from config import Config

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, config: Config, held_tickers: set[str]) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._notional_usd = config.trade_amount_usd
        self._held_tickers = held_tickers

    def buy(self, ticker: str) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
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
            logger.info(
                "BUY order accepted for %s $%.2f — order %s (pending fill)",
                ticker, self._notional_usd, getattr(order, "id", "unknown"),
            )
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)

    def sell(self, ticker: str) -> None:
        if ticker not in self._held_tickers:
            logger.warning("Sell called for %s but not in held_tickers — skipping", ticker)
            return
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            logger.info("SELL %s — position closed", ticker)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status in (404, 422):
                # Position already gone — clean up local state
                self._held_tickers.discard(ticker)
                logger.warning("SELL %s — position not found (status %s), removing from held", ticker, status)
            else:
                logger.error("Failed to sell %s: %s", ticker, e)
        except Exception as e:
            logger.error("Failed to sell %s: %s", ticker, e)
