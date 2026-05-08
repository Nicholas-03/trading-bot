import sqlite3

import analytics.server as server
from analytics.db import TradeDB


def test_recent_decisions_use_primary_without_trade_and_render_skip(tmp_path):
    db_path = tmp_path / "trades.db"
    db = TradeDB(str(db_path))
    try:
        news_id = db.record_news("2026-05-01T14:00:00Z", "AAPL headline", None, ["AAPL"])
        primary_id = db.record_decision(
            news_id, "2026-05-01T14:00:01Z", "buy", "AAPL", "chatgpt says buy",
            provider="chatgpt", confidence=0.5, is_primary=True,
        )
        db.record_skip(primary_id, "confidence_below_threshold")
    finally:
        db.close()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        _, recent = server._query_charts(con)
    finally:
        con.close()

    assert len(recent) == 1
    assert recent[0]["decision_id"] == primary_id
    assert recent[0]["provider"] == "chatgpt"
    assert recent[0]["skip_reason"] == "confidence_below_threshold"
    assert "confidence_below_threshold" in server._render_table_rows(recent)
