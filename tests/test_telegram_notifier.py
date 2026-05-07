import asyncio
import inspect
import pytest
from unittest.mock import AsyncMock, patch
from notifications.telegram_notifier import TelegramCommandListener, TelegramLogHandler, TelegramNotifier, NoOpNotifier


class _FakeExecutor:
    def __init__(self) -> None:
        self.trading_paused = False
        self.held_tickers = frozenset({"AAPL"})
        self.shorted_tickers = frozenset()
        self.sell = AsyncMock()


def _listener(chat_id: str = "123") -> TelegramCommandListener:
    listener = TelegramCommandListener.__new__(TelegramCommandListener)
    listener._chat_id = chat_id
    listener._order_executor = _FakeExecutor()
    listener._awaiting_sellall_confirm = False
    listener._reply = AsyncMock()
    listener._send_status = AsyncMock()
    return listener


def _update(text: str, chat_id: int | str) -> dict:
    return {"message": {"text": text, "chat": {"id": chat_id}}}


# --- NoOpNotifier ---

def test_noop_notifier_does_nothing():
    notifier = NoOpNotifier()
    # All methods must be awaitable and produce no side effects
    asyncio.run(notifier.notify_buy("AAPL", 5.0, "order-1"))
    asyncio.run(notifier.notify_sell("AAPL"))
    asyncio.run(notifier.notify_short("AAPL", 1, "order-2"))
    asyncio.run(notifier.notify_error("buy AAPL", "some error"))
    asyncio.run(notifier.notify_eod_report(1, 2, 3.0))
    asyncio.run(notifier.notify_weekly_report(1, 2, 3.0))
    asyncio.run(notifier.aclose())


def test_notifier_method_signatures_match_noop():
    for name in (
        "notify_buy",
        "notify_sell",
        "notify_short",
        "notify_error",
        "notify_eod_report",
        "notify_weekly_report",
        "aclose",
    ):
        assert inspect.signature(getattr(TelegramNotifier, name)) == inspect.signature(getattr(NoOpNotifier, name))


# --- TelegramNotifier message formatting ---

def test_format_buy():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_buy("AAPL", 5.0)
    assert "✅ BUY filled" in msg
    assert "AAPL" in msg
    assert "$5.00" in msg


def test_format_sell():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_sell("AAPL")
    assert "🔴 SELL executed" in msg
    assert "AAPL" in msg


def test_format_short():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_short("TSLA", 2, "xyz789")
    assert "🩳 SHORT filled" in msg
    assert "TSLA" in msg
    assert "2" in msg
    assert "xyz789" in msg


def test_format_error():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_error("buy AAPL", "Connection refused")
    assert "❌ ERROR" in msg
    assert "buy AAPL" in msg
    assert "Connection refused" in msg


def test_notify_buy_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.run(n.notify_buy("AAPL", 5.0, "abc123"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "AAPL" in message
    assert "$5.00" in message


def test_notify_sell_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.run(n.notify_sell("MSFT"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "MSFT" in message


def test_notify_short_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.run(n.notify_short("TSLA", 2, "xyz789"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "TSLA" in message
    assert "2" in message


def test_notify_error_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.run(n.notify_error("buy AAPL", "Connection refused"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "buy AAPL" in message
    assert "Connection refused" in message


def test_send_failure_does_not_raise():
    """A Telegram API failure must never propagate to the caller."""
    import httpx
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "tok"
    n._chat_id = "123"
    n._client = AsyncMock()
    n._client.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    # Should not raise
    asyncio.run(n._send("hello"))


def test_log_handler_caps_pending_tasks():
    async def run_case():
        loop = asyncio.get_running_loop()
        handler = TelegramLogHandler("tok", "123", loop, max_pending=1)
        handler._send = AsyncMock()
        handler._pending.add(asyncio.Future())
        record = logging_record("boom")

        handler.emit(record)

        handler._send.assert_not_called()
        for fut in list(handler._pending):
            fut.cancel()
        await handler.aclose()

    asyncio.run(run_case())


def logging_record(message: str):
    import logging
    return logging.LogRecord("test", logging.ERROR, __file__, 1, message, (), None)


# --- TelegramCommandListener authorization ---

def test_command_listener_accepts_configured_chat():
    listener = _listener(chat_id="123")

    asyncio.run(listener._handle_update(_update("/off", 123)))

    assert listener._order_executor.trading_paused is True
    listener._reply.assert_awaited_once()


def test_command_listener_ignores_wrong_chat():
    listener = _listener(chat_id="123")

    asyncio.run(listener._handle_update(_update("/off", 999)))

    assert listener._order_executor.trading_paused is False
    listener._reply.assert_not_awaited()


def test_command_listener_ignores_wrong_chat_confirmation():
    listener = _listener(chat_id="123")
    listener._awaiting_sellall_confirm = True

    asyncio.run(listener._handle_update(_update("/confirm", 999)))

    assert listener._awaiting_sellall_confirm is True
    listener._order_executor.sell.assert_not_awaited()
    listener._reply.assert_not_awaited()
