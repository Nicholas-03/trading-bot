import asyncio
import logging
import time
import httpx
import pytz
from datetime import datetime
from typing import Protocol, runtime_checkable

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None) -> None: ...
    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None: ...
    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None) -> None: ...
    async def notify_error(self, action: str, detail: str) -> None: ...
    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None: ...
    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None: ...
    async def aclose(self) -> None: ...

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"
_UPDATES_URL = "https://api.telegram.org/bot{token}/getUpdates"
_ET = pytz.timezone("America/New_York")


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    # --- Public notify methods ---

    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None) -> None:
        await self._send(self._format_buy(ticker, notional, order_id, fill_price))

    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        await self._send(self._format_sell(ticker, pnl_pct, pnl_usd))

    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None) -> None:
        await self._send(self._format_short(ticker, qty, order_id, fill_price))

    async def notify_error(self, action: str, detail: str) -> None:
        await self._send(self._format_error(action, detail))

    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
        await self._send(self._format_eod_report(buys, sells, pnl, datetime.now(_ET)))

    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
        await self._send(self._format_weekly_report(buys, sells, pnl, datetime.now(_ET)))

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Message formatters ---

    def _format_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None) -> str:
        msg = (
            f"✅ BUY filled\n"
            f"📌 Ticker: {ticker}\n"
            f"💵 Notional: ${notional:.2f}\n"
        )
        if fill_price:
            msg += f"💲 Fill: ${fill_price:.2f}/share\n"
        msg += f"🔖 Order ID: {order_id}"
        return msg

    def _format_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> str:
        msg = f"🔴 SELL executed\n📌 Ticker: {ticker}"
        if pnl_pct is not None and pnl_usd is not None:
            sign = "+" if pnl_usd >= 0 else ""
            msg += f"\n📊 P&L: {sign}{pnl_pct * 100:.2f}% ({sign}${pnl_usd:.2f})"
        return msg

    def _format_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None) -> str:
        msg = (
            f"🩳 SHORT filled\n"
            f"📌 Ticker: {ticker}\n"
            f"🔢 Qty: {qty}\n"
        )
        if fill_price:
            msg += f"💲 Fill: ${fill_price:.2f}/share\n"
        msg += f"🔖 Order ID: {order_id}"
        return msg

    def _format_error(self, action: str, detail: str) -> str:
        return (
            f"❌ ERROR\n"
            f"📌 Action: {action}\n"
            f"⚠️ Detail: {detail}"
        )

    def _format_eod_report(self, buys: int, sells: int, pnl: float, now_et: datetime) -> str:
        day_str = f"{now_et.strftime('%a %b')} {now_et.day}"
        sign = "+" if pnl >= 0 else ""
        return (
            f"📊 End of Day Report — {day_str}\n"
            f"🟢 Buys: {buys}\n"
            f"🔴 Sells: {sells}\n"
            f"💰 Day P&L: {sign}${pnl:.2f}"
        )

    def _format_weekly_report(self, buys: int, sells: int, pnl: float, now_et: datetime) -> str:
        day_str = f"{now_et.strftime('%b')} {now_et.day}"
        sign = "+" if pnl >= 0 else ""
        return (
            f"📅 Weekly Report — Week of {day_str}\n"
            f"🟢 Buys: {buys}\n"
            f"🔴 Sells: {sells}\n"
            f"💰 Week P&L: {sign}${pnl:.2f}"
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


class TelegramCommandListener:
    """Polls Telegram for incoming commands and handles /status."""

    def __init__(self, token: str, chat_id: str, order_executor: object) -> None:
        self._token = token
        self._chat_id = chat_id
        self._order_executor = order_executor
        self._client = httpx.AsyncClient(timeout=35.0)
        self._offset = 0
        self._started_at = time.monotonic()

    async def run(self) -> None:
        logger.info("Telegram command listener started")
        while True:
            try:
                await self._poll()
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.warning("Telegram poll error: %s", e)
                await asyncio.sleep(5)

    async def _poll(self) -> None:
        response = await self._client.get(
            _UPDATES_URL.format(token=self._token),
            params={"offset": self._offset, "timeout": 30, "allowed_updates": ["message"]},
        )
        response.raise_for_status()
        data = response.json()
        for update in data.get("result", []):
            self._offset = update["update_id"] + 1
            await self._handle_update(update)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message", {})
        text = message.get("text", "").strip()
        if text == "/status":
            await self._send_status()

    async def _send_status(self) -> None:
        uptime_secs = int(time.monotonic() - self._started_at)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        held = sorted(self._order_executor.held_tickers)
        shorted = sorted(self._order_executor.shorted_tickers)
        positions_str = ""
        if held:
            positions_str += f"\n📈 Long: {', '.join(held)}"
        if shorted:
            positions_str += f"\n📉 Short: {', '.join(shorted)}"
        if not held and not shorted:
            positions_str = "\n📭 No open positions"

        message = (
            f"✅ Service is online\n"
            f"⏱ Uptime: {uptime_str}"
            f"{positions_str}"
        )
        try:
            response = await self._client.post(
                _API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message},
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send status reply: %s", e)

    async def aclose(self) -> None:
        await self._client.aclose()


class NoOpNotifier(TelegramNotifier):
    """Drop-in stub used when TELEGRAM_ENABLED=false."""

    def __init__(self) -> None:
        pass  # No HTTP client needed

    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None) -> None:
        pass

    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        pass

    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None) -> None:
        pass

    async def notify_error(self, action: str, detail: str) -> None:
        pass

    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
        pass

    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
        pass

    async def _send(self, message: str) -> None:
        pass

    async def aclose(self) -> None:
        pass
