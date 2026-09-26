import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from advisor.laya_advisor import Decision
from analytics.db import TradeDB
from news.news_handler import NewsHandler, compute_news_age_hours


def _config(**overrides):
    config = MagicMock()
    config.news_stale_hours = 24.0
    config.min_confidence = 0.7
    config.allow_short = False
    config.default_hold_hours = 4
    config.max_hold_hours = 4
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _handler(decision: Decision, db=None, **config_overrides):
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    advisor = MagicMock()
    advisor.analyze = AsyncMock(return_value=decision)
    executor = MagicMock()
    executor.held_tickers = frozenset()
    executor.shorted_tickers = frozenset()
    executor.buy = AsyncMock()
    executor.short = AsyncMock()
    executor.sell = AsyncMock()
    handler = NewsHandler(client, _config(**config_overrides), advisor, executor, db)
    return handler, advisor, executor


def _news(headline="Company news", symbols=("AAPL",), created_at=None):
    return SimpleNamespace(
        headline=headline,
        summary="details",
        symbols=list(symbols),
        created_at=created_at or datetime.now(timezone.utc),
    )


def test_laya_decision_id_is_used_for_buy(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        handler, _, executor = _handler(
            Decision("buy", "AAPL", "probs", 0.95, 2, provider="laya", latency_sec=0.2),
            db,
        )

        asyncio.run(handler._handle_news(_news()))

        rows = db._conn.execute(
            "SELECT id, provider, is_primary, latency_sec, cost_usd FROM llm_decisions ORDER BY id"
        ).fetchall()
        assert [(r[1], r[2], r[3], r[4]) for r in rows] == [("laya", 1, 0.2, None)]
        executor.buy.assert_awaited_once()
        assert executor.buy.await_args.kwargs["decision_id"] == rows[0][0]
        assert executor.buy.await_args.kwargs["hold_hours"] == 2
    finally:
        db.close()


def test_buy_hold_hours_is_capped():
    handler, _, executor = _handler(Decision("buy", "BA", "probs", 0.95, 24))

    asyncio.run(handler._handle_news(_news(symbols=["BA"])))

    executor.buy.assert_awaited_once()
    assert executor.buy.await_args.kwargs["hold_hours"] == 4


def test_news_without_catalyst_still_reaches_advisor():
    handler, advisor, executor = _handler(Decision("buy", "NVDA", "probs", 0.95, 1))

    asyncio.run(handler._handle_news(_news("Analyst Raises NVDA Price Target To $180", ["NVDA"])))

    advisor.analyze.assert_awaited_once()
    executor.buy.assert_awaited_once()


def test_advisor_receives_news_and_positions():
    handler, advisor, _ = _handler(Decision("hold", None, "none", 0.0, 0))

    asyncio.run(handler._handle_news(_news(symbols=["AAPL", "MSFT"])))

    kwargs = advisor.analyze.await_args.kwargs
    assert kwargs["symbols"] == ["AAPL", "MSFT"]
    assert kwargs["held_tickers"] == frozenset()
    assert kwargs["shorted_tickers"] == frozenset()


def test_low_confidence_buy_is_skipped():
    handler, _, executor = _handler(Decision("buy", "AAPL", "probs", 0.5, 1))

    asyncio.run(handler._handle_news(_news()))

    executor.buy.assert_not_awaited()


def test_low_confidence_sell_is_skipped():
    handler, _, executor = _handler(Decision("sell", "AAPL", "probs", 0.5, 0))

    asyncio.run(handler._handle_news(_news()))

    executor.sell.assert_not_awaited()


def test_confident_sell_closes_position():
    handler, _, executor = _handler(Decision("sell", "AAPL", "probs", 0.9, 0))

    asyncio.run(handler._handle_news(_news()))

    executor.sell.assert_awaited_once_with("AAPL")


def test_short_skipped_when_disabled():
    handler, _, executor = _handler(Decision("short", "SPY", "probs", 0.9, 1))

    asyncio.run(handler._handle_news(_news(symbols=["SPY"])))

    executor.short.assert_not_awaited()


def test_stale_news_is_skipped():
    handler, advisor, _ = _handler(
        Decision("buy", "AAPL", "probs", 0.95, 1), news_stale_hours=2.0
    )
    old = datetime.now(timezone.utc) - timedelta(hours=3)

    asyncio.run(handler._handle_news(_news(created_at=old)))

    advisor.analyze.assert_not_awaited()


def test_news_without_symbols_is_skipped():
    handler, advisor, _ = _handler(Decision("buy", "AAPL", "probs", 0.95, 1))

    asyncio.run(handler._handle_news(_news(symbols=[])))

    advisor.analyze.assert_not_awaited()


# --- compute_news_age_hours ---

def test_age_zero_for_just_published():
    now = datetime.now(timezone.utc)
    assert compute_news_age_hours(now) < 0.1


def test_age_two_hours():
    two_ago = datetime.now(timezone.utc) - timedelta(hours=2)
    age = compute_news_age_hours(two_ago)
    assert 1.9 < age < 2.1


def test_age_naive_datetime_raises():
    naive = datetime(2026, 5, 1, 12, 0, 0)
    with pytest.raises(ValueError, match="timezone-aware"):
        compute_news_age_hours(naive)
