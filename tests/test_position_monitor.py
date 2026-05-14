import pytest
import asyncio
import pytz
from datetime import date, datetime
from unittest.mock import MagicMock
from trading.position_monitor import (
    PositionMonitor,
    compute_pnl_pct,
    _poll_error_delay,
    _should_fire_report,
    _should_log_poll_error_at_error,
)


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
    now = _ET.localize(datetime(2026, 4, 14, 16, 2, 0))
    assert _should_fire_report(now, None) is False


def test_should_fire_new_day_after_previous_report():
    now = _ET.localize(datetime(2026, 4, 15, 16, 0, 0))
    assert _should_fire_report(now, date(2026, 4, 14)) is True


def test_should_fire_report_raises_on_naive_datetime():
    naive = datetime(2026, 4, 14, 16, 0, 0)  # no tzinfo
    with pytest.raises(ValueError, match="timezone-aware"):
        _should_fire_report(naive, None)


def test_should_fire_at_16_01():
    now = _ET.localize(datetime(2026, 4, 14, 16, 1, 30))
    assert _should_fire_report(now, None) is True


def test_should_not_fire_on_weekend():
    # April 18, 2026 is a Saturday
    now = _ET.localize(datetime(2026, 4, 18, 16, 0, 0))
    assert _should_fire_report(now, None) is False


def test_fetch_eod_data_prefers_tradier_gain_loss_for_pnl():
    client = MagicMock()
    client.trade_activity_summary_for_date.return_value = (4, 10)
    client.gain_loss_summary_for_close_date.return_value = (10, 28.07)
    executor = MagicMock()
    executor.daily_summary.return_value = (14, 14, 17.27)
    monitor = PositionMonitor(client, MagicMock(), executor, MagicMock())

    buys, sells, pnl = monitor._fetch_eod_data()

    assert buys == 4
    assert sells == 10
    assert pnl == 28.07


def test_fetch_eod_data_falls_back_to_db_when_gain_loss_unavailable():
    client = MagicMock()
    client.trade_activity_summary_for_date.side_effect = RuntimeError("sandbox history unavailable")
    client.gain_loss_summary_for_close_date.side_effect = RuntimeError("gainloss unavailable")
    executor = MagicMock()
    executor.daily_summary.return_value = (14, 14, 17.27)
    monitor = PositionMonitor(client, MagicMock(), executor, MagicMock())

    assert monitor._fetch_eod_data() == (14, 14, 17.27)


def test_record_account_value_snapshot_writes_to_db():
    client = MagicMock()
    client.get_account_total_value.return_value = 25075.5
    db = MagicMock()
    monitor = PositionMonitor(client, MagicMock(), MagicMock(), MagicMock(), db)

    asyncio.run(monitor._record_account_value_snapshot())

    db.record_account_value.assert_called_once()
    _, value = db.record_account_value.call_args.args
    assert value == 25075.5


def test_latest_prices_uses_alpaca_market_data_not_tradier_quotes():
    client = MagicMock()
    client.get_quotes.return_value = {"AAPL": 1.0}
    alpaca = MagicMock()
    alpaca.get_latest_prices.return_value = {"AAPL": 175.25}
    monitor = PositionMonitor(client, MagicMock(), MagicMock(), MagicMock(), market_data_client=alpaca)

    prices = asyncio.run(monitor._latest_prices(["AAPL"]))

    assert prices == {"AAPL": 175.25}
    alpaca.get_latest_prices.assert_called_once_with(["AAPL"])
    client.get_quotes.assert_not_called()


def test_poll_error_delay_backs_off_to_cap():
    assert _poll_error_delay(1) == 30
    assert _poll_error_delay(2) == 60
    assert _poll_error_delay(5) == 300
    assert _poll_error_delay(20) == 300


def test_poll_error_logging_is_throttled():
    assert _should_log_poll_error_at_error(1) is True
    assert _should_log_poll_error_at_error(2) is False
    assert _should_log_poll_error_at_error(10) is True
