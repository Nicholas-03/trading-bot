import asyncio
import pytest
import pytz
from datetime import date, datetime
from unittest.mock import MagicMock, AsyncMock, patch
from trading.position_monitor import compute_pnl_pct, PositionMonitor, _should_fire_report


def test_pnl_at_stop_loss_boundary():
    # exactly -5% → should trigger stop-loss
    assert compute_pnl_pct(100.0, 95.0) == pytest.approx(-0.05)


def test_pnl_below_stop_loss():
    assert compute_pnl_pct(100.0, 90.0) == pytest.approx(-0.10)


def test_pnl_at_take_profit_boundary():
    # exactly +10% → should trigger take-profit
    assert compute_pnl_pct(100.0, 110.0) == pytest.approx(0.10)


def test_pnl_above_take_profit():
    assert compute_pnl_pct(100.0, 115.0) == pytest.approx(0.15)


def test_pnl_flat():
    assert compute_pnl_pct(50.0, 50.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# PDT guard tests for PositionMonitor._check_positions
# ---------------------------------------------------------------------------

def _make_monitor(stop_loss=0.02, take_profit=0.03):
    config = MagicMock()
    config.alpaca_api_key = "key"
    config.alpaca_secret_key = "secret"
    config.paper = True
    config.stop_loss_pct = stop_loss
    config.take_profit_pct = take_profit

    executor = MagicMock()
    executor.sell = AsyncMock()

    notifier = MagicMock()
    notifier.notify_eod_report = AsyncMock()
    notifier.notify_weekly_report = AsyncMock()

    client = MagicMock()
    monitor = PositionMonitor(client, config, executor, notifier)

    return monitor, executor


def _make_position(symbol, avg_entry_price, current_price):
    pos = MagicMock()
    pos.symbol = symbol
    pos.avg_entry_price = str(avg_entry_price)
    pos.current_price = str(current_price)
    pos.unrealized_pl = str(current_price - avg_entry_price)
    return pos


def test_stop_loss_pdt_guard_skips_sell():
    """Stop-loss triggered but position opened today → sell must NOT be called."""
    monitor, executor = _make_monitor(stop_loss=0.02, take_profit=0.10)
    # P&L of -3% exceeds stop-loss of 2%
    monitor._client.get_all_positions.return_value = [_make_position("AAPL", 100.0, 97.0)]
    executor.is_opened_today.return_value = True

    asyncio.run(monitor._check_positions())

    executor.sell.assert_not_called()


def test_stop_loss_no_pdt_guard_calls_sell():
    """Stop-loss triggered and position NOT opened today → sell must be called."""
    monitor, executor = _make_monitor(stop_loss=0.02, take_profit=0.10)
    monitor._client.get_all_positions.return_value = [_make_position("AAPL", 100.0, 97.0)]
    executor.is_opened_today.return_value = False

    asyncio.run(monitor._check_positions())

    executor.sell.assert_called_once_with("AAPL", pnl_pct=pytest.approx(-0.03), pnl_usd=pytest.approx(-3.0))


def test_take_profit_pdt_guard_skips_sell():
    """Take-profit triggered but position opened today → sell must NOT be called."""
    monitor, executor = _make_monitor(stop_loss=0.05, take_profit=0.03)
    # P&L of +5% exceeds take-profit of 3%
    monitor._client.get_all_positions.return_value = [_make_position("TSLA", 100.0, 105.0)]
    executor.is_opened_today.return_value = True

    asyncio.run(monitor._check_positions())

    executor.sell.assert_not_called()


def test_take_profit_no_pdt_guard_calls_sell():
    """Take-profit triggered and position NOT opened today → sell must be called."""
    monitor, executor = _make_monitor(stop_loss=0.05, take_profit=0.03)
    monitor._client.get_all_positions.return_value = [_make_position("TSLA", 100.0, 105.0)]
    executor.is_opened_today.return_value = False

    asyncio.run(monitor._check_positions())

    executor.sell.assert_called_once_with("TSLA", pnl_pct=pytest.approx(0.05), pnl_usd=pytest.approx(5.0))


# ---------------------------------------------------------------------------
# _should_fire_report tests
# ---------------------------------------------------------------------------

_ET = pytz.timezone("America/New_York")


def test_should_fire_at_market_close_no_prior_report():
    now = _ET.localize(datetime(2026, 4, 14, 16, 0, 30))
    assert _should_fire_report(now, None) is True


def test_should_not_fire_already_fired_today():
    now = _ET.localize(datetime(2026, 4, 14, 16, 0, 30))
    assert _should_fire_report(now, date(2026, 4, 14)) is False


def test_should_not_fire_before_close():
    now = _ET.localize(datetime(2026, 4, 14, 15, 59, 59))
    assert _should_fire_report(now, None) is False


def test_should_not_fire_after_close_minute():
    now = _ET.localize(datetime(2026, 4, 14, 16, 1, 0))
    assert _should_fire_report(now, None) is False


def test_should_fire_new_day_after_previous_report():
    now = _ET.localize(datetime(2026, 4, 15, 16, 0, 0))
    assert _should_fire_report(now, date(2026, 4, 14)) is True


def test_should_fire_report_raises_on_naive_datetime():
    naive = datetime(2026, 4, 14, 16, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        _should_fire_report(naive, None)
