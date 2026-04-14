import asyncio
import pytest
from unittest.mock import MagicMock, AsyncMock, patch
from trading.position_monitor import compute_pnl_pct, PositionMonitor


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

    with patch("trading.position_monitor.TradingClient"):
        monitor = PositionMonitor(config, executor)

    return monitor, executor


def _make_position(symbol, avg_entry_price, current_price):
    pos = MagicMock()
    pos.symbol = symbol
    pos.avg_entry_price = str(avg_entry_price)
    pos.current_price = str(current_price)
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

    executor.sell.assert_called_once_with("AAPL")


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

    executor.sell.assert_called_once_with("TSLA")
