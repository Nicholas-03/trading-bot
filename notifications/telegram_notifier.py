import asyncio
import logging
import time
import httpx
import pytz
from datetime import datetime
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trading.order_executor import OrderExecutor

logger = logging.getLogger(__name__)


@runtime_checkable
class Notifier(Protocol):
    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None: ...
    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None: ...
    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None: ...
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

    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None:
        await self._send(self._format_buy(ticker, notional, fill_price, fill_latency_sec))

    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        await self._send(self._format_sell(ticker, pnl_pct, pnl_usd))

    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None:
        await self._send(self._format_short(ticker, qty, order_id, fill_price, fill_latency_sec))

    async def notify_error(self, action: str, detail: str) -> None:
        await self._send(self._format_error(action, detail))

    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
        await self._send(self._format_eod_report(buys, sells, pnl, datetime.now(_ET)))

    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
        await self._send(self._format_weekly_report(buys, sells, pnl, datetime.now(_ET)))

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Message formatters ---

    def _format_buy(self, ticker: str, notional: float, fill_price: float | None = None, fill_latency_sec: float | None = None) -> str:
        msg = (
            f"✅ BUY filled\n"
            f"📌 Ticker: {ticker}\n"
            f"💵 Notional: ${notional:.2f}\n"
        )
        if fill_price:
            msg += f"💲 Fill: ${fill_price:.2f}/share\n"
        if fill_latency_sec is not None:
            msg += f"⏱ Fill time: {fill_latency_sec:.1f}s"
        return msg.rstrip("\n")

    def _format_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> str:
        msg = f"🔴 SELL executed\n📌 Ticker: {ticker}"
        if pnl_pct is not None and pnl_usd is not None:
            sign = "+" if pnl_usd >= 0 else ""
            msg += f"\n📊 Bot round-trip est.: {sign}{pnl_pct * 100:.2f}% ({sign}${pnl_usd:.2f})"
        return msg

    def _format_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> str:
        msg = (
            f"🩳 SHORT filled\n"
            f"📌 Ticker: {ticker}\n"
            f"🔢 Qty: {qty}\n"
        )
        if fill_price:
            msg += f"💲 Fill: ${fill_price:.2f}/share\n"
        if fill_latency_sec is not None:
            msg += f"⏱ Fill time: {fill_latency_sec:.1f}s\n"
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
            f"💰 Broker realized P&L: {sign}${pnl:.2f}"
        )

    def _format_weekly_report(self, buys: int, sells: int, pnl: float, now_et: datetime) -> str:
        day_str = f"{now_et.strftime('%b')} {now_et.day}"
        sign = "+" if pnl >= 0 else ""
        return (
            f"📅 Weekly Report — Week of {day_str}\n"
            f"🟢 Buys: {buys}\n"
            f"🔴 Sells: {sells}\n"
            f"💰 Broker realized P&L: {sign}${pnl:.2f}"
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

    def __init__(self, token: str, chat_id: str, order_executor: "OrderExecutor") -> None:
        self._token = token
        self._chat_id = chat_id
        self._order_executor = order_executor
        self._client = httpx.AsyncClient(timeout=35.0)
        self._offset = 0
        self._started_at = time.monotonic()
        self._awaiting_sellall_confirm: bool = False

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

    _HELP_TEXT = (
        "Available commands:\n"
        "/status — show uptime and open positions\n"
        "/off — pause trading (no new buys or shorts)\n"
        "/on — resume trading\n"
        "/sellall — close ALL open positions (asks for confirmation)\n"
        "/help — show this message"
    )

    def _is_authorized_message(self, message: dict) -> bool:
        chat = message.get("chat")
        if not isinstance(chat, dict):
            return False
        return str(chat.get("id")) == str(self._chat_id)

    async def _handle_update(self, update: dict) -> None:
        message = update.get("message", {})
        if not self._is_authorized_message(message):
            chat_id = message.get("chat", {}).get("id") if isinstance(message.get("chat"), dict) else None
            logger.warning("Ignoring Telegram command from unauthorized chat_id=%s", chat_id)
            return

        text = message.get("text", "").strip()

        if self._awaiting_sellall_confirm:
            if text == "/confirm":
                self._awaiting_sellall_confirm = False
                await self._execute_sellall()
            elif text == "/cancel":
                self._awaiting_sellall_confirm = False
                await self._reply("❌ Sell-all cancelled.")
            else:
                await self._reply(
                    "⚠️ Waiting for sell-all confirmation.\n"
                    "Reply /confirm to proceed or /cancel to abort."
                )
            return

        if text == "/status":
            await self._send_status()
        elif text == "/off":
            self._order_executor.trading_paused = True
            await self._reply("⏸ Trading paused — no new buys or shorts will be placed.")
        elif text == "/on":
            self._order_executor.trading_paused = False
            await self._reply("▶️ Trading resumed.")
        elif text == "/sellall":
            await self._prompt_sellall()
        elif text == "/help":
            await self._reply(self._HELP_TEXT)

    async def _prompt_sellall(self) -> None:
        held = sorted(self._order_executor.held_tickers)
        shorted = sorted(self._order_executor.shorted_tickers)
        if not held and not shorted:
            await self._reply("📭 No open positions to sell.")
            return
        lines = ["⚠️ Emergency sell-all requested!\n\nThis will close:"]
        if held:
            lines.append(f"📈 Long: {', '.join(held)}")
        if shorted:
            lines.append(f"📉 Short: {', '.join(shorted)}")
        lines.append("\nReply /confirm to execute or /cancel to abort.")
        self._awaiting_sellall_confirm = True
        await self._reply("\n".join(lines))

    async def _execute_sellall(self) -> None:
        held = list(self._order_executor.held_tickers)
        shorted = list(self._order_executor.shorted_tickers)
        all_tickers = held + shorted
        if not all_tickers:
            await self._reply("📭 No open positions found.")
            return
        await self._reply(f"🚨 Closing {len(all_tickers)} position(s)...")
        await asyncio.gather(
            *(self._order_executor.sell(ticker) for ticker in all_tickers),
            return_exceptions=True,
        )
        await self._reply("✅ Sell-all complete.")

    async def _reply(self, message: str) -> None:
        try:
            response = await self._client.post(
                _API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message},
            )
            response.raise_for_status()
        except Exception as e:
            logger.warning("Failed to send reply: %s", e)

    async def _send_status(self) -> None:
        uptime_secs = int(time.monotonic() - self._started_at)
        hours, remainder = divmod(uptime_secs, 3600)
        minutes, seconds = divmod(remainder, 60)
        uptime_str = f"{hours}h {minutes}m {seconds}s"

        held = sorted(self._order_executor.held_tickers)
        shorted = sorted(self._order_executor.shorted_tickers)
        paused = self._order_executor.trading_paused
        positions_str = ""
        if held:
            positions_str += f"\n📈 Long: {', '.join(held)}"
        if shorted:
            positions_str += f"\n📉 Short: {', '.join(shorted)}"
        if not held and not shorted:
            positions_str = "\n📭 No open positions"

        status_str = "⏸ PAUSED" if paused else "✅ Active"
        message = (
            f"🤖 Bot status: {status_str}\n"
            f"⏱ Uptime: {uptime_str}"
            f"{positions_str}"
        )
        await self._reply(message)

    async def aclose(self) -> None:
        await self._client.aclose()


class TelegramLogHandler(logging.Handler):
    """Logging handler that forwards ERROR+ records to a Telegram chat."""

    def __init__(self, token: str, chat_id: str, loop: asyncio.AbstractEventLoop, max_pending: int = 100) -> None:
        super().__init__(level=logging.ERROR)
        self._token = token
        self._chat_id = chat_id
        self._loop = loop
        self._client = httpx.AsyncClient(timeout=10.0)
        self._max_pending = max_pending
        self._pending: set[asyncio.Future] = set()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            if self._loop.is_closed() or len(self._pending) >= self._max_pending:
                return
            parts = [f"🚨 {record.levelname} [{record.name}]", record.getMessage()]
            if record.exc_info and record.exc_info[0]:
                exc_val = str(record.exc_info[1]) if record.exc_info[1] else ""
                exc_line = record.exc_info[0].__name__
                if exc_val:
                    exc_line += f": {exc_val}"
                parts.append(exc_line)
            text = "\n".join(parts)
            future = asyncio.run_coroutine_threadsafe(self._send(text), self._loop)
            self._pending.add(future)
            future.add_done_callback(self._pending.discard)
        except Exception:
            self.handleError(record)

    async def _send(self, message: str) -> None:
        try:
            await self._client.post(
                _API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message},
            )
        except Exception:
            pass  # never log from inside a log handler

    async def aclose(self) -> None:
        if self._pending:
            await asyncio.wait(
                [asyncio.wrap_future(f) for f in list(self._pending)],
                timeout=2.0,
            )
        await self._client.aclose()


class NoOpNotifier:
    """Drop-in stub used when TELEGRAM_ENABLED=false."""

    async def notify_buy(self, ticker: str, notional: float, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None:
        pass

    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        pass

    async def notify_short(self, ticker: str, qty: int, order_id: str, fill_price: float | None = None, fill_latency_sec: float | None = None) -> None:
        pass

    async def notify_error(self, action: str, detail: str) -> None:
        pass

    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
        pass

    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
        pass

    async def aclose(self) -> None:
        pass
