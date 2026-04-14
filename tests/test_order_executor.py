from datetime import date
from unittest.mock import MagicMock, patch, AsyncMock
import pytest
from trading.order_executor import OrderExecutor
from config import Config


def _make_executor() -> OrderExecutor:
    config = MagicMock(spec=Config)
    config.alpaca_api_key = "key"
    config.alpaca_secret_key = "secret"
    config.paper = True
    config.trade_amount_usd = 5.0
    config.short_qty = 1
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    with patch("trading.order_executor.TradingClient"):
        return OrderExecutor(config, set(), set(), notifier)


def test_is_opened_today_false_when_not_tracked():
    ex = _make_executor()
    assert ex.is_opened_today("AAPL") is False


def test_is_opened_today_true_after_recording_today():
    ex = _make_executor()
    ex._open_dates["AAPL"] = date.today()
    assert ex.is_opened_today("AAPL") is True


def test_is_opened_today_false_for_yesterday():
    from datetime import timedelta
    ex = _make_executor()
    ex._open_dates["AAPL"] = date.today() - timedelta(days=1)
    assert ex.is_opened_today("AAPL") is False


def test_open_date_cleared_on_sell():
    ex = _make_executor()
    ex._open_dates["AAPL"] = date.today()
    ex._held_tickers.add("AAPL")
    ex._client.close_position = MagicMock()

    import asyncio
    asyncio.run(ex.sell("AAPL"))

    assert "AAPL" not in ex._open_dates
