import pytest
import pytz
from datetime import date, datetime
from trading.position_monitor import compute_pnl_pct, _should_fire_report


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
