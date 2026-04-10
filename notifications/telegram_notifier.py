import logging
import httpx
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None: ...
    async def notify_sell(self, ticker: str) -> None: ...
    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None: ...
    async def notify_error(self, action: str, detail: str) -> None: ...
    async def aclose(self) -> None: ...

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    # --- Public notify methods ---

    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None:
        await self._send(self._format_buy(ticker, notional, order_id))

    async def notify_sell(self, ticker: str) -> None:
        await self._send(self._format_sell(ticker))

    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None:
        await self._send(self._format_short(ticker, qty, order_id))

    async def notify_error(self, action: str, detail: str) -> None:
        await self._send(self._format_error(action, detail))

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Message formatters ---

    def _format_buy(self, ticker: str, notional: float, order_id: str) -> str:
        return (
            f"✅ BUY executed\n"
            f"📌 Ticker: {ticker}\n"
            f"💵 Notional: ${notional:.2f}\n"
            f"🔖 Order ID: {order_id}"
        )

    def _format_sell(self, ticker: str) -> str:
        return (
            f"🔴 SELL executed\n"
            f"📌 Ticker: {ticker}"
        )

    def _format_short(self, ticker: str, qty: int, order_id: str) -> str:
        return (
            f"🩳 SHORT executed\n"
            f"📌 Ticker: {ticker}\n"
            f"🔢 Qty: {qty}\n"
            f"🔖 Order ID: {order_id}"
        )

    def _format_error(self, action: str, detail: str) -> str:
        return (
            f"❌ ERROR\n"
            f"📌 Action: {action}\n"
            f"⚠️ Detail: {detail}"
        )

    # --- HTTP transport ---

    async def _send(self, message: str) -> None:
        try:
            response = await self._client.post(
                _API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message},
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("Telegram notification failed: %s", e)


class NoOpNotifier(TelegramNotifier):
    """Drop-in stub used when TELEGRAM_ENABLED=false."""

    def __init__(self) -> None:
        pass  # No HTTP client needed

    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None:
        pass

    async def notify_sell(self, ticker: str) -> None:
        pass

    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None:
        pass

    async def notify_error(self, action: str, detail: str) -> None:
        pass

    async def _send(self, message: str) -> None:
        pass

    async def aclose(self) -> None:
        pass
