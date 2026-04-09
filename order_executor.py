import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from config import Config

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, config: Config, held_tickers: set[str]) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._trade_amount = config.trade_amount_usd
        self._held_tickers = held_tickers

    def buy(self, ticker: str) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    notional=self._trade_amount,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._held_tickers.add(ticker)
            logger.info("BUY %s $%.2f — order %s", ticker, self._trade_amount, order.id)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)

    def sell(self, ticker: str) -> None:
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            logger.info("SELL %s — position closed", ticker)
        except Exception as e:
            logger.error("Failed to sell %s: %s", ticker, e)
