# tests/test_order_executor.py
import asyncio
from datetime import date, datetime, timedelta, timezone
from unittest.mock import MagicMock, AsyncMock
import time
import pytest
from trading.order_executor import OrderExecutor, _monday_of
from trading.tradier_client import MarketBar, TradierOrder, TradierPosition
from config import Config


def _make_executor(market_data_client=None) -> OrderExecutor:
    client = MagicMock()
    config = MagicMock(spec=Config)
    config.trade_amount_usd = 100.0
    config.short_qty = 1
    config.stop_loss_pct = 0.02
    config.take_profit_pct = 0.03
    config.max_slippage_pct = 0.005
    config.extended_move_low_price_pct = 0.15
    config.extended_move_any_pct = 0.10
    config.entry_confirmation_enabled = False
    config.entry_confirmation_lookback_minutes = 8
    config.entry_confirmation_trend_minutes = 3
    config.entry_confirmation_max_fade_pct = 0.015
    config.entry_confirmation_max_quote_premium_pct = 0.01
    config.fast_fail_enabled = True
    config.fast_fail_minutes = 5
    config.fast_fail_loss_pct = 0.015
    config.fast_fail_min_favorable_pct = 0.0025
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    return OrderExecutor(client, config, set(), set(), notifier, market_data_client=market_data_client)


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


# --- sell path counter / P&L accumulation (synchronous state only) ---

def test_sell_increments_daily_sells():
    """Verify _daily_sells increments when sell() successfully closes."""
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._client.close_position = MagicMock(return_value="order-1")
    ex._client.get_quotes = MagicMock(return_value={"AAPL": 155.0})
    ex._client.get_order = MagicMock(return_value=("filled", 155.0))
    ex._position_book["AAPL"] = (150.0, 1, None)

    import asyncio
    asyncio.run(ex.sell("AAPL"))

    _, sells, _ = ex.daily_summary()
    assert sells == 1


def test_sell_accumulates_realized_pnl():
    """Verify realized P&L is computed and accumulated when entry price is known."""
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._client.close_position = MagicMock(return_value="order-1")
    ex._client.get_quotes = MagicMock(return_value={"AAPL": 160.0})
    ex._client.get_order = MagicMock(return_value=("filled", 160.0))
    ex._position_book["AAPL"] = (150.0, 2, None)  # 2 shares, entry $150, exit $160 → $20 P&L

    import asyncio
    asyncio.run(ex.sell("AAPL"))

    _, _, pnl = ex.daily_summary()
    assert abs(pnl - 20.0) < 0.01


def test_sell_skips_pnl_when_already_provided():
    """When pnl_usd is provided by caller (e.g. PositionMonitor), use it directly."""
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._client.close_position = MagicMock(return_value="order-1")
    ex._client.get_quotes = MagicMock(return_value={"AAPL": 999.0})  # should NOT be used
    ex._client.get_order = MagicMock(return_value=("filled", None))
    ex._position_book["AAPL"] = (150.0, 1, None)

    import asyncio
    asyncio.run(ex.sell("AAPL", pnl_pct=0.05, pnl_usd=7.50))

    _, _, pnl = ex.daily_summary()
    assert abs(pnl - 7.50) < 0.01
    # get_quotes should not have been called since pnl_usd was provided
    ex._client.get_quotes.assert_not_called()


# --- _wait_for_fill ---

def test_wait_for_fill_returns_true_on_filled():
    ex = _make_executor()
    ex._client.get_order = MagicMock(return_value=("filled", 175.5))

    import asyncio
    filled, price = asyncio.run(ex._wait_for_fill("order-1"))
    assert filled is True
    assert price == 175.5


def test_wait_for_fill_returns_false_on_rejected():
    ex = _make_executor()
    ex._client.get_order = MagicMock(return_value=("rejected", None))

    import asyncio
    filled, price = asyncio.run(ex._wait_for_fill("order-1"))
    assert filled is False
    assert price is None


def test_wait_for_fill_returns_false_on_timeout():
    ex = _make_executor()
    ex._client.get_order = MagicMock(return_value=("open", None))

    import asyncio
    filled, price = asyncio.run(ex._wait_for_fill("order-1", timeout_sec=0.1, poll_interval=0.05))
    assert filled is False
    assert price is None


def test_wait_for_position_accepts_delayed_fill():
    ex = _make_executor()
    ex._client.get_order = MagicMock(return_value=("open", None))
    ex._client.get_all_positions = MagicMock(side_effect=[
        [],
        [TradierPosition(symbol="AAPL", qty=2.0, cost_basis=101.0)],
    ])

    filled, price = asyncio.run(ex._wait_for_position("AAPL", "otoco-1", timeout_sec=0.2, poll_interval=0.01))

    assert filled is True
    assert price == 50.5
    assert ex._client.get_all_positions.call_count == 2


def test_wait_for_position_survives_transient_status_read_failure():
    ex = _make_executor()
    ex._client.get_order = MagicMock(side_effect=[RuntimeError("temporary status read failure")])
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="AAPL", qty=1.0, cost_basis=100.0)
    ])

    filled, price = asyncio.run(ex._wait_for_position("AAPL", "otoco-1", timeout_sec=0.1, poll_interval=0.01))

    assert filled is True
    assert price == 100.0


def test_wait_for_position_times_out_without_position_or_terminal_order():
    ex = _make_executor()
    ex._client.get_order = MagicMock(return_value=("open", None))
    ex._client.get_all_positions = MagicMock(return_value=[])

    filled, price = asyncio.run(ex._wait_for_position("AAPL", "otoco-1", timeout_sec=0.03, poll_interval=0.01))

    assert filled is False
    assert price is None


# --- buy rollback on unconfirmed fill ---

def test_entry_confirmation_unavailable_records_skip_before_order_submission():
    ex = _make_executor()
    ex._entry_confirmation_enabled = True
    ex._db = MagicMock()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"EEX": (20.0, 19.5)})
    ex._client.get_intraday_bars = MagicMock(return_value=[])
    ex._client.submit_otoco_order = MagicMock(return_value="should-not-submit")

    asyncio.run(ex.buy("EEX", decision_id=406))

    ex._db.record_skip.assert_called_once_with(406, "entry_confirmation_unavailable")
    ex._client.submit_otoco_order.assert_not_called()
    ex._db.record_trade_open.assert_not_called()


def test_buy_rolls_back_state_on_unconfirmed_fill():
    """If a buy order fails to confirm, the ticker must be removed from held_tickers
    and the buy counter must not be left inflated."""
    ex = _make_executor()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (100.0, 95.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("rejected", None))
    ex._client.get_all_positions = MagicMock(return_value=[])
    ex._client.cancel_order = MagicMock()

    import asyncio
    asyncio.run(ex.buy("AAPL"))

    assert "AAPL" not in ex.held_tickers
    assert "AAPL" not in ex._position_book
    assert "AAPL" not in ex._daily_bought_tickers
    buys, _, _ = ex.daily_summary()
    assert buys == 0


def test_buy_unconfirmed_after_submission_does_not_record_skip_reason():
    ex = _make_executor()
    ex._db = MagicMock()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (100.0, 95.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="otoco-ambiguous")
    ex._wait_for_position = AsyncMock(return_value=(False, None))
    ex._client.get_account_orders = MagicMock(return_value=[])
    ex._client.cancel_order = MagicMock()

    asyncio.run(ex.buy("AAPL", decision_id=77))

    ex._db.record_skip.assert_not_called()
    ex._db.record_trade_open.assert_not_called()
    ex._client.cancel_order.assert_called_once_with("otoco-ambiguous")


def test_buy_exception_after_order_submission_does_not_record_skip_reason():
    ex = _make_executor()
    ex._db = MagicMock()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (100.0, 95.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="otoco-1")
    ex._wait_for_position = AsyncMock(side_effect=RuntimeError("polling crashed"))

    asyncio.run(ex.buy("AAPL", decision_id=88))

    ex._db.record_skip.assert_not_called()
    ex._notifier.notify_error.assert_called_once()


def test_buy_exception_records_skip_reason():
    import asyncio
    ex = _make_executor()
    ex._db = MagicMock()
    ex._client.get_buying_power = MagicMock(side_effect=RuntimeError("broker down"))

    asyncio.run(ex.buy("AAPL", decision_id=77))

    ex._db.record_skip.assert_called_once_with(77, "buy_exception")
    ex._notifier.notify_error.assert_called_once()


# --- short rollback on unconfirmed fill ---

def test_short_rolls_back_state_on_unconfirmed_fill():
    """If a short order fails to confirm, the ticker must be removed from shorted_tickers
    and the buy counter must not be left inflated."""
    ex = _make_executor()
    ex._client.submit_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("rejected", None))

    import asyncio
    asyncio.run(ex.short("AAPL"))

    assert "AAPL" not in ex.shorted_tickers
    assert "AAPL" not in ex._position_book
    buys, _, _ = ex.daily_summary()
    assert buys == 0


# --- decision_monotonic / fill_latency_sec ---

def test_buy_with_decision_monotonic_calculates_correct_latency():
    """Verify fill_latency_sec is calculated from decision_monotonic, not just submission time."""
    ex = _make_executor()

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (50.0, 47.0)})  # Lower price so qty=2
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 52.0))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="AAPL", qty=2.0, cost_basis=104.0)
    ])

    import asyncio

    # Capture decision time, then run buy with a slight delay to simulate decision processing
    decision_time = time.monotonic()
    time.sleep(0.05)  # Simulate 50ms of decision processing

    asyncio.run(ex.buy("AAPL", decision_id=1, decision_monotonic=decision_time))

    # Check that notify_buy was called
    assert ex._notifier.notify_buy.called, "notify_buy was not called"
    
    # notify_buy is called with keyword args
    call_kwargs = ex._notifier.notify_buy.call_args.kwargs
    fill_latency_sec = call_kwargs.get('fill_latency_sec')

    # Should be at least 50ms (the sleep time)
    assert fill_latency_sec is not None, "fill_latency_sec was not passed to notify_buy"
    assert fill_latency_sec >= 0.045, f"Expected >= 0.045, got {fill_latency_sec}"


def test_buy_without_decision_monotonic_uses_submission_time():
    """When decision_monotonic is None, fallback to measuring from submission time."""
    ex = _make_executor()

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (50.0, 47.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 52.0))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="AAPL", qty=2.0, cost_basis=104.0)
    ])

    import asyncio

    # Call buy without decision_monotonic
    asyncio.run(ex.buy("AAPL", decision_id=1))

    assert ex._notifier.notify_buy.called
    call_kwargs = ex._notifier.notify_buy.call_args.kwargs
    fill_latency_sec = call_kwargs.get('fill_latency_sec')

    # Should be close to zero (no delay between submission and fill in this mock)
    assert fill_latency_sec is not None
    assert fill_latency_sec < 0.1


def test_buy_records_bracket_order_id_in_db():
    import asyncio
    ex = _make_executor()
    ex._db = MagicMock()
    ex._db.record_trade_open.return_value = 321
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (50.0, 49.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="otoco-321")
    ex._client.get_order = MagicMock(return_value=("filled", 50.0))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="AAPL", qty=2.0, cost_basis=100.0)
    ])

    asyncio.run(ex.buy("AAPL", decision_id=7))

    args = ex._db.record_trade_open.call_args.args
    assert args[0] == 7
    assert args[1] == "AAPL"
    assert args[8] == "otoco-321"


def test_short_with_decision_monotonic_calculates_correct_latency():
    """Verify short() also correctly calculates fill_latency_sec from decision_monotonic."""
    ex = _make_executor()

    ex._client.submit_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 155.0))

    import asyncio

    decision_time = time.monotonic()
    time.sleep(0.05)

    asyncio.run(ex.short("AAPL", decision_id=2, decision_monotonic=decision_time))

    assert ex._notifier.notify_short.called
    call_kwargs = ex._notifier.notify_short.call_args.kwargs
    fill_latency_sec = call_kwargs.get('fill_latency_sec')

    # Should include the sleep time
    assert fill_latency_sec is not None
    assert fill_latency_sec >= 0.045, f"Expected >= 0.045, got {fill_latency_sec}"


def test_decision_monotonic_not_included_in_db_when_db_disabled():
    """When analytics DB is disabled (db=None), buy still works but decision_monotonic has no effect on DB."""
    ex = _make_executor()
    ex._db = None  # No DB

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"AAPL": (50.0, 47.0)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 52.0))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="AAPL", qty=2.0, cost_basis=104.0)
    ])

    import asyncio

    decision_time = time.monotonic()
    time.sleep(0.05)

    # Should complete without error
    asyncio.run(ex.buy("AAPL", decision_monotonic=decision_time))

    # Verify ticker was added to held
    assert "AAPL" in ex.held_tickers


# --- same-day re-entry guard ---

def test_buy_blocked_after_same_day_stop_loss():
    """After a stop loss on ticker X, a new buy for X the same day must be blocked."""
    import asyncio
    ex = _make_executor()
    ex._daily_stopped_tickers.add("UONE")

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"UONE": (7.0, 6.0)})
    ex._client.submit_order = MagicMock(return_value="order-1")

    asyncio.run(ex.buy("UONE"))

    ex._client.submit_order.assert_not_called()
    assert "UONE" not in ex.held_tickers


def test_buy_blocked_on_same_day_duplicate():
    """After a successful buy + close of ticker X today, a second buy must be blocked."""
    import asyncio
    ex = _make_executor()
    ex._daily_bought_tickers.add("ARVN")

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"ARVN": (10.5, 9.8)})
    ex._client.submit_order = MagicMock(return_value="order-1")

    asyncio.run(ex.buy("ARVN"))

    ex._client.submit_order.assert_not_called()


def test_buy_allowed_for_fresh_ticker_despite_other_stops():
    """A stop on UONE must not block a buy for a completely different ticker."""
    import asyncio
    ex = _make_executor()
    ex._daily_stopped_tickers.add("UONE")

    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"ARVN": (10.5, 9.8)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 10.5))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="ARVN", qty=9.0, cost_basis=94.5)
    ])

    asyncio.run(ex.buy("ARVN"))

    ex._client.submit_otoco_order.assert_called_once()


def test_daily_stopped_tickers_resets_on_new_day():
    ex = _make_executor()
    ex._daily_stopped_tickers.add("UONE")
    ex._daily_bought_tickers.add("UONE")
    ex._last_day = date.today() - timedelta(days=1)
    ex._maybe_reset_day()
    assert "UONE" not in ex._daily_stopped_tickers
    assert "UONE" not in ex._daily_bought_tickers


def test_stop_loss_close_adds_to_daily_stopped_tickers():
    """Closing a position with exit_reason='stop_loss' must add ticker to _daily_stopped_tickers."""
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("MX")
    ex._position_book["MX"] = (3.70, 13, None)
    ex._client.close_position = MagicMock(return_value="order-1")
    ex._client.get_quotes = MagicMock(return_value={"MX": 3.57})
    ex._client.get_order = MagicMock(return_value=("filled", 3.57))

    asyncio.run(ex.sell("MX", exit_reason="stop_loss"))

    assert "MX" in ex._daily_stopped_tickers


def test_take_profit_close_does_not_add_to_stopped_tickers():
    """A take_profit close must NOT add to _daily_stopped_tickers."""
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("ARVN")
    ex._position_book["ARVN"] = (10.29, 4, None)
    ex._client.close_position = MagicMock(return_value="order-1")
    ex._client.get_quotes = MagicMock(return_value={"ARVN": 11.10})
    ex._client.get_order = MagicMock(return_value=("filled", 11.10))

    asyncio.run(ex.sell("ARVN", exit_reason="take_profit"))

    assert "ARVN" not in ex._daily_stopped_tickers


# --- intraday extension filter ---

def test_buy_blocked_low_price_extended_move():
    """Price < $5 and intraday move > 15% must block the buy."""
    import asyncio
    ex = _make_executor()
    # TELA: open $0.90, last $1.10 → +22% > 15%
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"TELA": (1.10, 0.90)})
    ex._client.submit_order = MagicMock(return_value="order-1")

    asyncio.run(ex.buy("TELA"))

    ex._client.submit_order.assert_not_called()


def test_buy_blocked_any_price_extreme_move():
    """Any price, intraday move > 20% must block the buy."""
    import asyncio
    ex = _make_executor()
    # UONE: open $5.62, last $7.14 → +27%
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"UONE": (7.14, 5.62)})
    ex._client.submit_order = MagicMock(return_value="order-1")

    asyncio.run(ex.buy("UONE"))

    ex._client.submit_order.assert_not_called()


def test_buy_allowed_moderate_move():
    """A 5% move from open must not trigger the extension block."""
    import asyncio
    ex = _make_executor()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"ARVN": (10.29, 9.80)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 10.29))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="ARVN", qty=9.0, cost_basis=92.61)
    ])

    asyncio.run(ex.buy("ARVN"))

    ex._client.submit_otoco_order.assert_called_once()


# --- falling on good news ---

def test_buy_blocked_when_stock_down_on_session():
    """Stock down more than 3% from session open while bot wants to buy → block."""
    import asyncio
    ex = _make_executor()
    # MRNA: open $47.15, last $44.68 → -5.2%
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"MRNA": (44.68, 47.15)})
    ex._client.submit_order = MagicMock(return_value="order-1")

    asyncio.run(ex.buy("MRNA"))

    ex._client.submit_order.assert_not_called()


def test_buy_allowed_slight_pullback():
    """A -1% intraday move is acceptable — must not trigger the falling-on-good-news block."""
    import asyncio
    ex = _make_executor()
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"INSG": (19.41, 19.60)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 19.41))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="INSG", qty=5.0, cost_basis=97.05)
    ])

    asyncio.run(ex.buy("INSG"))

    ex._client.submit_otoco_order.assert_called_once()


# --- 1-minute entry confirmation ---

def _bars(closes: list[float], highs: list[float] | None = None) -> list[MarketBar]:
    highs = highs or closes
    return [
        MarketBar(
            time=f"2026-05-07T10:{idx:02d}:00",
            open=close,
            high=high,
            low=min(close, high),
            close=close,
            volume=1000,
        )
        for idx, (close, high) in enumerate(zip(closes, highs), start=1)
    ]


def test_entry_confirmation_blocks_faded_spike():
    ex = _make_executor()
    ex._entry_confirmation_enabled = True

    reason = ex._entry_confirmation_skip_reason(
        "GPGI",
        13.45,
        _bars(
            [13.84, 13.83, 13.75, 13.72, 13.73, 13.62, 13.51, 13.45],
            [13.90, 13.90, 13.84, 13.76, 13.75, 13.72, 13.62, 13.53],
        ),
    )

    assert reason == "faded_spike_block"


def test_entry_confirmation_blocks_weak_followthrough():
    ex = _make_executor()
    ex._entry_confirmation_enabled = True

    reason = ex._entry_confirmation_skip_reason(
        "GPGI",
        13.45,
        _bars([13.50, 13.55, 13.52, 13.51, 13.48, 13.46, 13.45, 13.44]),
    )

    assert reason == "weak_followthrough_block"


def test_entry_confirmation_blocks_quote_premium():
    ex = _make_executor()
    ex._entry_confirmation_enabled = True

    reason = ex._entry_confirmation_skip_reason(
        "GPGI",
        14.05,
        _bars([13.50, 13.55, 13.57, 13.58, 13.60, 13.62, 13.64, 13.66]),
    )

    assert reason == "quote_premium_block"


def test_recent_bars_prefers_alpaca_market_data():
    alpaca = MagicMock()
    alpaca.get_intraday_bars.return_value = _bars([100, 101, 102, 103])
    ex = _make_executor(market_data_client=alpaca)
    ex._client.get_intraday_bars.return_value = _bars([90, 91, 92, 93])

    bars = asyncio.run(ex._recent_bars("AAPL", 8))

    assert [bar.close for bar in bars] == [100, 101, 102, 103]
    alpaca.get_intraday_bars.assert_called_once()
    ex._client.get_intraday_bars.assert_not_called()


def test_recent_bars_falls_back_to_tradier_when_alpaca_has_too_few_bars():
    alpaca = MagicMock()
    alpaca.get_intraday_bars.return_value = _bars([100, 101])
    ex = _make_executor(market_data_client=alpaca)
    ex._client.get_intraday_bars.return_value = _bars([90, 91, 92, 93])

    bars = asyncio.run(ex._recent_bars("AAPL", 8))

    assert [bar.close for bar in bars] == [90, 91, 92, 93]
    alpaca.get_intraday_bars.assert_called_once()
    ex._client.get_intraday_bars.assert_called_once()


def test_fast_fail_triggers_only_before_meaningful_favorable_move():
    ex = _make_executor()
    ex._position_book["HIMX"] = (17.47, 28, None)
    ex._hold_opened_at["HIMX"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    assert ex.update_price_for_fast_fail("HIMX", 17.18) is True


def test_fast_fail_does_not_trigger_after_favorable_move():
    ex = _make_executor()
    ex._position_book["HIMX"] = (17.47, 28, None)
    ex._hold_opened_at["HIMX"] = datetime.now(timezone.utc) - timedelta(minutes=2)

    assert ex.update_price_for_fast_fail("HIMX", 17.55) is False
    assert ex.update_price_for_fast_fail("HIMX", 17.18) is False


# --- limit order dispatch ---

def test_buy_uses_limit_entry_for_low_price_stock():
    """Price < $5 → OTOCO must be placed with a limit entry price (slippage cap)."""
    import asyncio
    ex = _make_executor()
    # FATN: open $2.99, last $3.18 → +6.4% (below 15% threshold) + price < 5
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"FATN": (3.18, 2.99)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 3.18))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="FATN", qty=31.0, cost_basis=98.58)
    ])

    asyncio.run(ex.buy("FATN"))

    call_args = ex._client.submit_otoco_order.call_args
    # submit_otoco_order(symbol, qty, tp_price, sl_price, entry_limit) — entry_limit is args[4]
    entry_limit = call_args.args[4] if len(call_args.args) > 4 else call_args.kwargs.get("entry_limit")
    assert entry_limit is not None
    assert entry_limit == pytest.approx(3.18 * 1.005, rel=1e-4)


def test_buy_uses_limit_entry_for_calm_large_cap():
    """High-price stock with modest intraday move still uses a capped limit entry."""
    import asyncio
    ex = _make_executor()
    # IONQ: open $44.16, last $46.05 → +4.3%, price > $5
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"IONQ": (46.05, 44.16)})
    ex._client.submit_otoco_order = MagicMock(return_value="order-1")
    ex._client.get_order = MagicMock(return_value=("filled", 46.05))
    ex._client.get_all_positions = MagicMock(return_value=[
        TradierPosition(symbol="IONQ", qty=2.0, cost_basis=92.10)
    ])

    asyncio.run(ex.buy("IONQ"))

    call_args = ex._client.submit_otoco_order.call_args
    entry_limit = call_args.args[4] if len(call_args.args) > 4 else call_args.kwargs.get("entry_limit")
    assert entry_limit == pytest.approx(46.05 * 1.005, rel=1e-4)


# --- handle_bracket_close ---

def test_handle_bracket_close_take_profit_uses_tp_price():
    """When price >= entry, bracket close must use the TP price for P&L, not live quote."""
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._position_book["AAPL"] = (100.0, 5, None)
    # config: tp=3%, sl=2% → TP=$103.00, SL=$98.00
    # live quote is $103.50 — above TP but should NOT be used for P&L
    asyncio.run(ex.handle_bracket_close("AAPL", 103.50))

    assert "AAPL" not in ex.held_tickers
    call_args = ex._notifier.notify_sell.call_args
    pnl_pct = call_args.args[1]
    assert abs(pnl_pct - 0.03) < 0.0001  # P&L must be exactly +3% (TP level), not +3.5%


def test_handle_bracket_close_stop_loss_uses_sl_price():
    """When price < entry, bracket close must use the SL price for P&L, not live quote."""
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._position_book["AAPL"] = (100.0, 5, None)
    # live quote is $97.00 — below entry; SL was at $98.00
    asyncio.run(ex.handle_bracket_close("AAPL", 97.00))

    assert "AAPL" not in ex.held_tickers
    call_args = ex._notifier.notify_sell.call_args
    pnl_pct = call_args.args[1]
    assert abs(pnl_pct - (-0.02)) < 0.0001  # P&L must be exactly -2% (SL level), not -3%


def test_handle_bracket_close_no_entry_price_skips_pnl():
    """When entry price is unknown (0.0), P&L stays None and reason stays 'bracket_order'."""
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex._position_book["AAPL"] = (0.0, 0, None)

    asyncio.run(ex.handle_bracket_close("AAPL", 105.0))

    assert "AAPL" not in ex.held_tickers
    call_args = ex._notifier.notify_sell.call_args
    pnl_pct = call_args.args[1]
    assert pnl_pct is None


def test_handle_bracket_close_writes_db_fields_from_exit_fill():
    import asyncio
    ex = _make_executor()
    ex._db = MagicMock()
    ex._held_tickers.add("AAPL")
    ex._position_book["AAPL"] = (100.0, 5, 123)
    ex._hold_opened_at["AAPL"] = ex._parse_tradier_dt("2026-05-01T14:00:00Z")
    ex._client.get_account_orders = MagicMock(return_value=[
        TradierOrder(
            symbol="AAPL",
            side="sell",
            status="filled",
            order_type="limit",
            avg_fill_price=103.0,
            filled_at="2026-05-01T15:00:00Z",
            quantity=5,
        )
    ])

    asyncio.run(ex.handle_bracket_close("AAPL", 104.0))

    ex._db.record_trade_close.assert_called_once_with(
        123, 103.0, 15.0, 0.03, "take_profit", "2026-05-01T15:00:00Z"
    )


def test_seed_from_db_restores_expired_hold_window_after_restart():
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex.seed_from_db([
        {
            "id": 123,
            "ticker": "AAPL",
            "side": "buy",
            "qty": 1,
            "entry_price": 100.0,
            "hold_hours": 1,
            "opened_at": "2000-01-01T00:00:00Z",
        }
    ])

    assert "AAPL" in ex.expired_hold_tickers()
    assert "AAPL" in ex._hold_opened_at


def test_seed_from_db_restores_bracket_order_id_after_restart():
    ex = _make_executor()
    ex._held_tickers.add("AAPL")
    ex.seed_from_db([
        {
            "id": 123,
            "ticker": "AAPL",
            "side": "buy",
            "qty": 1,
            "entry_price": 100.0,
            "hold_hours": 0,
            "opened_at": "2026-05-01T00:00:00Z",
            "bracket_order_id": "otoco-123",
        }
    ])

    assert ex._bracket_orders["AAPL"] == "otoco-123"


def test_sell_does_not_submit_duplicate_when_market_close_pending():
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("DD")
    ex._position_book["DD"] = (48.87, 1, 123)
    ex._client.get_account_orders = MagicMock(return_value=[
        TradierOrder(
            symbol="DD",
            side="sell",
            status="pending",
            order_type="market",
            avg_fill_price=None,
            filled_at=None,
            quantity=1,
            order_id="sell-1",
        )
    ])
    ex._client.close_position = MagicMock(return_value="sell-2")

    asyncio.run(ex.sell("DD", exit_reason="hold_hours"))

    ex._client.close_position.assert_not_called()
    assert "DD" in ex.pending_close
    ex._notifier.notify_error.assert_called_once()


def test_sell_cancels_active_bracket_close_legs_when_parent_id_missing():
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("KHC")
    ex._position_book["KHC"] = (22.92, 2, 123)
    ex._client.get_quotes = MagicMock(return_value={"KHC": 22.50})
    ex._client.get_account_orders = MagicMock(side_effect=[
        [
            TradierOrder(
                symbol="KHC",
                side="sell",
                status="open",
                order_type="limit",
                avg_fill_price=None,
                filled_at=None,
                quantity=2,
                order_id="tp-1",
            ),
            TradierOrder(
                symbol="KHC",
                side="sell",
                status="open",
                order_type="stop",
                avg_fill_price=None,
                filled_at=None,
                quantity=2,
                order_id="sl-1",
            ),
        ],
        [],
        [],
    ])
    ex._client.cancel_order = MagicMock()
    ex._client.close_position = MagicMock(return_value="sell-1")
    ex._wait_for_fill = AsyncMock(return_value=(True, 22.50))

    asyncio.run(ex.sell("KHC", exit_reason="hold_hours"))

    ex._client.cancel_order.assert_any_call("tp-1")
    ex._client.cancel_order.assert_any_call("sl-1")
    ex._client.close_position.assert_called_once_with("KHC")
    assert "KHC" not in ex.held_tickers
    ex._notifier.notify_sell.assert_called_once()


def test_sell_defers_when_bracket_cancel_is_still_pending():
    import asyncio
    ex = _make_executor()
    ex._held_tickers.add("KHC")
    ex._position_book["KHC"] = (22.92, 2, 123)
    blocking = TradierOrder(
        symbol="KHC",
        side="sell",
        status="pending",
        order_type="stop",
        avg_fill_price=None,
        filled_at=None,
        quantity=2,
        order_id="sl-1",
    )
    ex._client.get_quotes = MagicMock(return_value={"KHC": 22.50})
    ex._client.get_account_orders = MagicMock(return_value=[blocking])
    ex._client.cancel_order = MagicMock()
    ex._client.close_position = MagicMock(return_value="sell-1")
    ex._wait_for_bracket_close_orders_clear = AsyncMock(return_value=[blocking])

    asyncio.run(ex.sell("KHC", exit_reason="hold_hours"))

    ex._client.cancel_order.assert_called_once_with("sl-1")
    ex._client.close_position.assert_not_called()
    assert "KHC" in ex.pending_close
    ex._notifier.notify_error.assert_not_called()


def test_sell_unconfirmed_does_not_notify_or_close_db():
    import asyncio
    ex = _make_executor()
    ex._db = MagicMock()
    ex._held_tickers.add("DD")
    ex._position_book["DD"] = (48.87, 1, 123)
    ex._client.get_quotes = MagicMock(return_value={"DD": 50.07})
    ex._client.close_position = MagicMock(return_value="sell-1")
    ex._wait_for_fill = AsyncMock(return_value=(False, None))

    asyncio.run(ex.sell("DD", exit_reason="hold_hours"))

    assert "DD" in ex.held_tickers
    ex._db.record_trade_close.assert_not_called()
    ex._notifier.notify_sell.assert_not_called()
    ex._notifier.notify_error.assert_called_once()


def test_buy_fast_bracket_round_trip_uses_tradier_fills():
    import asyncio
    ex = _make_executor()
    ex._db = MagicMock()
    ex._db.record_trade_open.return_value = 42
    ex._client.get_buying_power = MagicMock(return_value=500.0)
    ex._client.get_quotes_with_open = MagicMock(return_value={"ZTEK": (0.56, 0.53)})
    ex._client.submit_otoco_order = MagicMock(return_value="otoco-1")
    ex._wait_for_position = AsyncMock(return_value=(False, None))
    ex._client.cancel_order = MagicMock()
    ex._client.get_account_orders = MagicMock(return_value=[
        TradierOrder(
            symbol="ZTEK",
            side="buy",
            status="filled",
            order_type="limit",
            avg_fill_price=0.51,
            filled_at="2999-05-06T16:33:27.434Z",
            quantity=89,
        ),
        TradierOrder(
            symbol="ZTEK",
            side="sell",
            status="filled",
            order_type="stop",
            avg_fill_price=0.49,
            filled_at="2999-05-06T16:33:27.513Z",
            quantity=89,
        ),
    ])

    asyncio.run(ex.buy("ZTEK", decision_id=4231, hold_hours=24))

    ex._client.cancel_order.assert_not_called()
    ex._notifier.notify_error.assert_not_called()
    ex._notifier.notify_buy.assert_called_once()
    ex._notifier.notify_sell.assert_called_once()
    sell_args = ex._notifier.notify_sell.call_args.args
    assert sell_args[0] == "ZTEK"
    assert abs(sell_args[1] - ((0.49 - 0.51) / 0.51)) < 1e-9
    assert abs(sell_args[2] - ((0.49 - 0.51) * 89)) < 1e-9
    ex._db.record_trade_close.assert_called_once()
    close_args = ex._db.record_trade_close.call_args.args
    assert close_args[1] == 0.49
    assert close_args[4] == "stop_loss"

