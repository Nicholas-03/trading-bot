from datetime import date, timedelta
from unittest.mock import MagicMock, AsyncMock
import pytest
from alpaca.common.exceptions import APIError
from trading.order_executor import OrderExecutor
from config import Config


def _make_executor(open_dates: dict | None = None) -> OrderExecutor:
    config = MagicMock(spec=Config)
    config.trade_amount_usd = 5.0
    config.short_qty = 1
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    client = MagicMock()
    return OrderExecutor(client, config, set(), set(), notifier, open_dates=open_dates)


def test_is_opened_today_false_when_not_tracked():
    ex = _make_executor()
    assert ex.is_opened_today("AAPL") is False


def test_is_opened_today_true_after_recording_today():
    ex = _make_executor()
    ex._open_dates["AAPL"] = date.today()
    assert ex.is_opened_today("AAPL") is True


def test_is_opened_today_false_for_yesterday():
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


def test_open_date_cleared_on_404_sell():
    ex = _make_executor()
    ex._open_dates["AAPL"] = date.today()
    ex._held_tickers.add("AAPL")

    http_err = MagicMock()
    http_err.response.status_code = 404
    err = APIError("position not found", http_error=http_err)
    ex._client.close_position = MagicMock(side_effect=err)

    import asyncio
    asyncio.run(ex.sell("AAPL"))

    assert "AAPL" not in ex._open_dates
    assert "AAPL" not in ex._held_tickers
