import asyncio
import logging
from trading.order_executor import OrderExecutor
from config import Config
from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


class PositionMonitor:
    def __init__(self, config: Config, order_executor: OrderExecutor) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor

    async def run(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")

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

                if pnl <= -self._stop_loss:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping stop-loss close for %s (opened today)", ticker)
                    else:
                        logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker)
                elif pnl >= self._take_profit:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping take-profit close for %s (opened today)", ticker)
                    else:
                        logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
