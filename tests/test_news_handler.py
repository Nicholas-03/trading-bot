import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from analytics.db import TradeDB
from llm.llm_advisor import Decision
from news.news_handler import NewsHandler, _format_entry_precheck_context


class Snapshot:
    def __init__(self, entry_price: float | None, spread_pct: float | None) -> None:
        self.entry_price = entry_price
        self.spread_pct = spread_pct


def test_chatgpt_decision_id_is_used_for_buy(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        client = MagicMock()
        client.get_clock.return_value = SimpleNamespace(is_open=True)
        config = MagicMock()
        config.news_stale_hours = 24.0
        config.min_confidence = 0.7
        config.allow_short = False
        config.default_hold_hours = 4
        config.max_hold_hours = 4
        config.require_hard_catalyst_news = True
        config.block_soft_partnership_news = True
        advisor = MagicMock()
        advisor.analyze = AsyncMock(
            return_value=Decision(
                "buy", "AAPL", "yes", 0.95, 2,
                provider="chatgpt", latency_sec=0.2, cost_usd=0.002,
            )
        )
        executor = MagicMock()
        executor.held_tickers = frozenset()
        executor.shorted_tickers = frozenset()
        executor.buy = AsyncMock()
        handler = NewsHandler(client, config, advisor, executor, db)
        news = SimpleNamespace(
            headline="AAPL wins $2B major contract",
            summary="The company signed a quantified enterprise contract.",
            symbols=["AAPL"],
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(handler._handle_news(news))

        rows = db._conn.execute(
            "SELECT id, provider, is_primary, latency_sec, cost_usd FROM llm_decisions ORDER BY id"
        ).fetchall()
        assert [(r[1], r[2], r[3], r[4]) for r in rows] == [("chatgpt", 1, 0.2, 0.002)]
        executor.buy.assert_awaited_once()
        assert executor.buy.await_args.kwargs["decision_id"] == rows[0][0]
        assert executor.buy.await_args.kwargs["hold_hours"] == 2
    finally:
        db.close()


def test_buy_hold_hours_is_capped(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        client = MagicMock()
        client.get_clock.return_value = SimpleNamespace(is_open=True)
        config = MagicMock()
        config.news_stale_hours = 24.0
        config.min_confidence = 0.7
        config.allow_short = False
        config.default_hold_hours = 4
        config.max_hold_hours = 4
        config.require_hard_catalyst_news = True
        config.block_soft_partnership_news = True
        advisor = MagicMock()
        advisor.analyze = AsyncMock(return_value=Decision("buy", "BA", "yes", 0.95, 24))
        executor = MagicMock()
        executor.held_tickers = frozenset()
        executor.shorted_tickers = frozenset()
        executor.buy = AsyncMock()
        handler = NewsHandler(client, config, advisor, executor, db)
        news = SimpleNamespace(
            headline="Boeing Wins $7.7B Aircraft Order",
            summary="details",
            symbols=["BA"],
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(handler._handle_news(news))

        executor.buy.assert_awaited_once()
        assert executor.buy.await_args.kwargs["hold_hours"] == 4
    finally:
        db.close()


def test_soft_partnership_without_materiality_is_skipped():
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    config = MagicMock()
    config.news_stale_hours = 24.0
    config.require_hard_catalyst_news = True
    config.block_soft_partnership_news = True
    advisor = MagicMock()
    advisor.analyze = AsyncMock(return_value=Decision("buy", "SAP", "yes", 0.95, 4))
    executor = MagicMock()
    executor.held_tickers = frozenset()
    executor.shorted_tickers = frozenset()
    executor.buy = AsyncMock()
    handler = NewsHandler(client, config, advisor, executor, None)
    news = SimpleNamespace(
        headline="SAP Invests In AI Platform N8n; Strikes Partnership To Embed Platform",
        summary="Strategic investment and multi-year commercial partnership.",
        symbols=["SAP"],
        created_at=datetime.now(timezone.utc),
    )

    asyncio.run(handler._handle_news(news))

    advisor.analyze.assert_not_awaited()
    executor.buy.assert_not_awaited()


def test_analyst_price_target_news_is_skipped():
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    config = MagicMock()
    config.news_stale_hours = 24.0
    config.require_hard_catalyst_news = True
    config.block_soft_partnership_news = True
    advisor = MagicMock()
    advisor.analyze = AsyncMock(return_value=Decision("buy", "NVDA", "yes", 0.95, 1))
    executor = MagicMock()
    executor.held_tickers = frozenset()
    executor.shorted_tickers = frozenset()
    executor.buy = AsyncMock()
    handler = NewsHandler(client, config, advisor, executor, None)
    news = SimpleNamespace(
        headline="Analyst Raises NVDA Price Target To $180",
        summary="The firm maintains a buy rating.",
        symbols=["NVDA"],
        created_at=datetime.now(timezone.utc),
    )

    asyncio.run(handler._handle_news(news))

    advisor.analyze.assert_not_awaited()
    executor.buy.assert_not_awaited()


def test_non_hard_catalyst_news_is_skipped():
    client = MagicMock()
    client.get_clock.return_value = SimpleNamespace(is_open=True)
    config = MagicMock()
    config.news_stale_hours = 24.0
    config.require_hard_catalyst_news = True
    config.block_soft_partnership_news = True
    advisor = MagicMock()
    advisor.analyze = AsyncMock(return_value=Decision("buy", "HAS", "yes", 0.95, 1))
    executor = MagicMock()
    executor.held_tickers = frozenset()
    executor.shorted_tickers = frozenset()
    executor.buy = AsyncMock()
    handler = NewsHandler(client, config, advisor, executor, None)
    news = SimpleNamespace(
        headline="Hasbro Magic Growth Shows No Signs Of Slowing",
        summary="Narrative commentary without a quantified catalyst.",
        symbols=["HAS"],
        created_at=datetime.now(timezone.utc),
    )

    asyncio.run(handler._handle_news(news))

    advisor.analyze.assert_not_awaited()
    executor.buy.assert_not_awaited()


def test_entry_precheck_context_classifies_tradable_and_blocked_symbols():
    context = _format_entry_precheck_context(
        ["AAPL", "PODC", "WIDE", "NOQ"],
        {
            "AAPL": Snapshot(190.0, 0.001),
            "PODC": Snapshot(4.5, 0.001),
            "WIDE": Snapshot(30.0, 0.012),
        },
        min_trade_price=20.0,
        max_entry_spread_pct=0.005,
    )

    assert "AAPL: price=$190.00 spread=0.10%" in context
    assert "PODC: low_price price=$4.50" in context
    assert "WIDE: wide_spread price=$30.00 spread=1.20%" in context
    assert "NOQ: no_quote" in context


def test_news_handler_passes_entry_precheck_context_to_advisor(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        client = MagicMock()
        client.get_clock.return_value = SimpleNamespace(is_open=True)
        config = MagicMock()
        config.news_stale_hours = 24.0
        config.min_confidence = 0.7
        config.min_trade_price = 20.0
        config.max_entry_spread_pct = 0.005
        config.allow_short = False
        config.default_hold_hours = 1
        config.max_hold_hours = 1
        config.require_hard_catalyst_news = True
        config.block_soft_partnership_news = True
        advisor = MagicMock()
        advisor.analyze = AsyncMock(return_value=Decision("hold", None, "blocked", 0.0, 0))
        executor = MagicMock()
        executor.held_tickers = frozenset()
        executor.shorted_tickers = frozenset()
        executor.buy = AsyncMock()
        market_data_client = MagicMock()
        market_data_client.get_snapshots.return_value = {
            "AAPL": Snapshot(190.0, 0.001),
            "TINY": Snapshot(3.0, 0.001),
        }
        handler = NewsHandler(client, config, advisor, executor, db, market_data_client)
        news = SimpleNamespace(
            headline="AAPL Wins $2B Cloud Contract",
            summary="The contract has a direct financial amount.",
            symbols=["AAPL", "TINY"],
            created_at=datetime.now(timezone.utc),
        )

        asyncio.run(handler._handle_news(news))

        context = advisor.analyze.await_args.kwargs["symbol_entry_context"]
        assert "AAPL: price=$190.00" in context
        assert "TINY: low_price price=$3.00" in context
    finally:
        db.close()
