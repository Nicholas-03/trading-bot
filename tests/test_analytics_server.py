import sqlite3
import pytest
from analytics.server import _query_decision, _query_stats


def _make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            symbols TEXT
        );
        CREATE TABLE llm_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_event_id INTEGER REFERENCES news_events(id),
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ticker TEXT,
            reasoning TEXT,
            confidence REAL DEFAULT 0.0,
            hold_hours INTEGER DEFAULT 0,
            provider TEXT,
            latency_sec REAL
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER REFERENCES llm_decisions(id),
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            entry_price REAL,
            exit_price REAL,
            pnl_usd REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            fill_latency_sec REAL,
            hold_hours INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        );
    """)
    return con


def test_query_decision_not_found():
    con = _make_db()
    assert _query_decision(con, 999) is None


def test_query_decision_returns_fields():
    con = _make_db()
    con.execute(
        "INSERT INTO news_events (ts, headline) VALUES ('2026-01-01T10:00:00', 'Fed raises rates')"
    )
    con.execute(
        "INSERT INTO llm_decisions (news_event_id, ts, action, ticker, reasoning, confidence, hold_hours) "
        "VALUES (1, '2026-01-01T10:00:01', 'buy', 'JPM', 'Banks benefit from rate hikes', 0.85, 2)"
    )
    con.commit()
    result = _query_decision(con, 1)
    assert result is not None
    assert result["headline"] == "Fed raises rates"
    assert result["ts"] == "2026-01-01T10:00:00"
    assert len(result["decisions"]) == 1
    d = result["decisions"][0]
    assert d["action"] == "buy"
    assert d["ticker"] == "JPM"
    assert d["confidence"] == pytest.approx(0.85)
    assert d["reasoning"] == "Banks benefit from rate hikes"
    assert d["hold_hours"] == 2


def test_query_stats_empty_db():
    con = _make_db()
    stats = _query_stats(con)
    assert stats["total"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl"] == 0.0
    assert stats["best"] is None
    assert stats["worst"] is None


def test_query_stats_with_trades():
    con = _make_db()
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('AAPL', 'buy', 10, 50.0, '2026-01-01', '2026-01-01')"
    )
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('GOOG', 'buy', 5, -20.0, '2026-01-02', '2026-01-02')"
    )
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('MSFT', 'buy', 3, 10.0, '2026-01-03', '2026-01-03')"
    )
    con.commit()
    stats = _query_stats(con)
    assert stats["total"] == 3
    assert stats["win_rate"] == pytest.approx(66.67, rel=0.01)
    assert stats["total_pnl"] == pytest.approx(40.0)
    assert stats["best"] == ("AAPL", 50.0)
    assert stats["worst"] == ("GOOG", -20.0)


def test_query_stats_ignores_open_trades():
    con = _make_db()
    # Trade with no closed_at is still open — must not count
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at) "
        "VALUES ('AAPL', 'buy', 10, 50.0, '2026-01-01')"
    )
    con.commit()
    stats = _query_stats(con)
    assert stats["total"] == 0
    assert stats["best"] is None


def test_query_decision_returns_all_siblings():
    con = _make_db()
    con.execute(
        "INSERT INTO news_events (ts, headline) VALUES ('2026-01-01T10:00:00', 'AAPL beats earnings')"
    )
    con.executemany(
        "INSERT INTO llm_decisions "
        "(news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec) "
        "VALUES (1, '2026-01-01T10:00:01', ?, ?, ?, ?, ?, ?, ?)",
        [
            ("buy",  "AAPL", "strong",  0.90, 2, "claude",   1.2),
            ("hold", None,   "unsure",  0.00, 0, "gemini",   0.8),
            ("buy",  "AAPL", "bullish", 0.75, 1, "deepseek", 2.1),
        ],
    )
    con.commit()

    result = _query_decision(con, 1)  # query using Claude's id
    assert result is not None
    assert result["headline"] == "AAPL beats earnings"
    assert len(result["decisions"]) == 3
    providers = [d["provider"] for d in result["decisions"]]
    assert providers == ["claude", "gemini", "deepseek"]
    assert result["decisions"][0]["action"] == "buy"
    assert result["decisions"][1]["action"] == "hold"
    assert abs(result["decisions"][0]["latency_sec"] - 1.2) < 0.001
