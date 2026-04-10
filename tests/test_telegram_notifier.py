import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from notifications.telegram_notifier import TelegramNotifier, NoOpNotifier


# --- NoOpNotifier ---

def test_noop_notifier_does_nothing():
    notifier = NoOpNotifier()
    # All methods must be awaitable and produce no side effects
    asyncio.run(notifier.notify_buy("AAPL", 5.0, "order-1"))
    asyncio.run(notifier.notify_sell("AAPL"))
    asyncio.run(notifier.notify_short("AAPL", 1, "order-2"))
    asyncio.run(notifier.notify_error("buy AAPL", "some error"))


# --- TelegramNotifier message formatting ---

def test_format_buy():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_buy("AAPL", 5.0, "abc123")
    assert "✅ BUY executed" in msg
    assert "AAPL" in msg
    assert "$5.00" in msg
    assert "abc123" in msg


def test_format_sell():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_sell("AAPL")
    assert "🔴 SELL executed" in msg
    assert "AAPL" in msg


def test_format_short():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_short("TSLA", 2, "xyz789")
    assert "🩳 SHORT executed" in msg
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
