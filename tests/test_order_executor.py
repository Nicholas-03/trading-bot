# tests/test_order_executor.py
from datetime import date, timedelta
from unittest.mock import MagicMock, AsyncMock
import pytest
from trading.order_executor import OrderExecutor, _monday_of
from config import Config


def _make_executor() -> OrderExecutor:
    client = MagicMock()
    config = MagicMock(spec=Config)
    config.trade_amount_usd = 100.0
    config.short_qty = 1
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    return OrderExecutor(client, config, set(), set(), notifier)


# --- _monday_of ---

def test_monday_of_monday():
    d = date(2026, 4, 13)  # Monday
    assert _monday_of(d) == date(2026, 4, 13)


def test_monday_of_friday():
    d = date(2026, 4, 17)  # Friday
    assert _monday_of(d) == date(2026, 4, 13)


def test_monday_of_sunday():
    d = date(2026, 4, 19)  # Sunday
    assert _monday_of(d) == date(2026, 4, 13)


# --- daily_summary ---

def test_daily_summary_initial_state():
    ex = _make_executor()
    buys, sells, pnl = ex.daily_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_daily_summary_resets_on_new_day():
    ex = _make_executor()
    ex._daily_buys = 3
    ex._daily_sells = 2
    ex._daily_realized_pnl = 12.50
    ex._last_day = date.today() - timedelta(days=1)  # simulate stale day
    buys, sells, pnl = ex.daily_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_daily_summary_does_not_reset_same_day():
    ex = _make_executor()
    ex._daily_buys = 2
    ex._daily_sells = 1
    ex._daily_realized_pnl = 5.0
    buys, sells, pnl = ex.daily_summary()
    assert buys == 2
    assert sells == 1
    assert pnl == 5.0


# --- weekly_summary ---

def test_weekly_summary_initial_state():
    ex = _make_executor()
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_weekly_summary_resets_on_new_week():
    ex = _make_executor()
    ex._weekly_buys = 5
    ex._weekly_sells = 3
    ex._weekly_realized_pnl = 25.0
    ex._last_week_monday = _monday_of(date.today()) - timedelta(weeks=1)  # last week
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_weekly_summary_does_not_reset_same_week():
    ex = _make_executor()
    ex._weekly_buys = 4
    ex._weekly_sells = 2
    ex._weekly_realized_pnl = 18.0
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 4
    assert sells == 2
    assert pnl == 18.0


# --- pending_close / confirm_closed ---

def test_confirm_closed_removes_ticker():
    ex = _make_executor()
    ex._pending_close.add("AAPL")
    ex.confirm_closed("AAPL")
    assert "AAPL" not in ex.pending_close


def test_confirm_closed_noop_for_unknown_ticker():
    ex = _make_executor()
    ex.confirm_closed("AAPL")  # should not raise
    assert "AAPL" not in ex.pending_close
