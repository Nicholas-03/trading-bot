import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

from analytics.db import TradeDB
from llm.llm_advisor import Decision
from llm.multi_advisor import MultiDecision, ProviderResult
from news.news_handler import NewsHandler


def test_multi_llm_primary_decision_id_is_used_for_buy(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        client = MagicMock()
        client.get_clock.return_value = SimpleNamespace(is_open=True)
        config = MagicMock()
        config.news_stale_hours = 24.0
        config.min_confidence = 0.7
        config.allow_short = False
        advisor = MagicMock()
        advisor.analyze = AsyncMock(return_value=MultiDecision(
            primary=Decision("buy", "AAPL", "chatgpt wins", 0.95, 2),
            primary_provider="chatgpt",
            all_results=[
                ProviderResult("claude", Decision("hold", None, "no", 0.0, 0), 0.1, 0.001),
                ProviderResult("chatgpt", Decision("buy", "AAPL", "yes", 0.95, 2), 0.2, 0.002),
            ],
        ))
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
            "SELECT id, provider, is_primary FROM llm_decisions ORDER BY id"
        ).fetchall()
        assert [(r[1], r[2]) for r in rows] == [("claude", 0), ("chatgpt", 1)]
        executor.buy.assert_awaited_once()
        assert executor.buy.await_args.kwargs["decision_id"] == rows[1][0]
        assert executor.buy.await_args.kwargs["hold_hours"] == 2
    finally:
        db.close()
