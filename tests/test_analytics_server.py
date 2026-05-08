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


def test_index_reads_fresh_db_rows_and_auto_refreshes(tmp_path):
    db_path = tmp_path / "trades.db"
    db = TradeDB(str(db_path))
    old_db_path = server.DB_PATH
    old_refresh_seconds = server.REFRESH_SECONDS
    server.DB_PATH = str(db_path)
    server.REFRESH_SECONDS = 1
    try:
        news_id = db.record_news("2026-05-01T14:00:00Z", "AAA first headline", None, ["AAA"])
        db.record_decision(news_id, "2026-05-01T14:00:01Z", "hold", "AAA", "first")

        first = server.index()
        first_body = first.body.decode()
        assert "AAA first headline" in first_body
        assert "BBB second headline" not in first_body
        assert "window.location.reload()" in first_body
        assert first.headers["cache-control"] == "no-store, max-age=0"

        news_id = db.record_news("2026-05-01T14:01:00Z", "BBB second headline", None, ["BBB"])
        db.record_decision(news_id, "2026-05-01T14:01:01Z", "hold", "BBB", "second")

        second_body = server.index().body.decode()
        assert "AAA first headline" in second_body
        assert "BBB second headline" in second_body
    finally:
        db.close()
        server.DB_PATH = old_db_path
        server.REFRESH_SECONDS = old_refresh_seconds
