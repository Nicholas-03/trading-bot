import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from analytics.db import TradeDB
from llm.llm_advisor import Decision
from news.news_handler import NewsHandler


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
            headline="AAPL wins major contract",
            summary="details",
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
