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


def test_index_reads_fresh_db_rows_without_browser_cache(tmp_path):
    db_path = tmp_path / "trades.db"
    db = TradeDB(str(db_path))
    old_db_path = server.DB_PATH
    server.DB_PATH = str(db_path)
    try:
        news_id = db.record_news("2026-05-01T14:00:00Z", "AAA first headline", None, ["AAA"])
        db.record_decision(news_id, "2026-05-01T14:00:01Z", "hold", "AAA", "first")

        first = server.index()
        first_body = first.body.decode()
        assert "AAA first headline" in first_body
        assert "BBB second headline" not in first_body
        assert "window.location.reload()" not in first_body
        assert first.headers["cache-control"] == "no-store, max-age=0"

        news_id = db.record_news("2026-05-01T14:01:00Z", "BBB second headline", None, ["BBB"])
        db.record_decision(news_id, "2026-05-01T14:01:01Z", "hold", "BBB", "second")

        second_body = server.index().body.decode()
        assert "AAA first headline" in second_body
        assert "BBB second headline" in second_body
    finally:
        db.close()
        server.DB_PATH = old_db_path


def test_recent_closed_trade_uses_closed_time_for_sort_and_display(tmp_path):
    db_path = tmp_path / "trades.db"
    db = TradeDB(str(db_path))
    try:
        older_news_id = db.record_news("2026-05-07T14:00:00Z", "MSFT older headline", None, ["MSFT"])
        decision_id = db.record_decision(
            older_news_id,
            "2026-05-07T14:00:01Z",
            "buy",
            "MSFT",
            "buy before close",
            is_primary=True,
        )
        trade_id = db.record_trade_open(
            decision_id,
            "MSFT",
            "buy",
            1,
            100.0,
            "2026-05-07T14:01:00Z",
        )
        db.record_trade_close(
            trade_id,
            101.0,
            1.0,
            0.01,
            "llm",
            "2026-05-08T15:00:00Z",
        )

        newer_news_id = db.record_news("2026-05-08T14:00:00Z", "AAPL newer headline", None, ["AAPL"])
        db.record_decision(
            newer_news_id,
            "2026-05-08T14:00:01Z",
            "hold",
            "AAPL",
            "hold after open",
            is_primary=True,
        )
    finally:
        db.close()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        _, recent = server._query_charts(con)
    finally:
        con.close()

    assert recent[0]["ticker"] == "MSFT"
    assert recent[0]["ts"] == "2026-05-08T15:00:00Z"
    assert 'data-closed="true"' in server._render_table_rows([recent[0]])


def test_account_value_chart_uses_snapshots(tmp_path):
    db_path = tmp_path / "trades.db"
    db = TradeDB(str(db_path))
    try:
        db.record_account_value("2026-05-09T10:00:00Z", 25000.0)
        db.record_account_value("2026-05-09T10:30:00Z", 25075.5)
    finally:
        db.close()

    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    try:
        charts, _ = server._query_charts(con)
    finally:
        con.close()

    assert "account_value" in charts
    assert charts["account_value"]["data"][0]["y"] == [25000.0, 25075.5]
    assert charts["account_value"]["layout"]["title"]["text"] == "Account Total Value Trend"
