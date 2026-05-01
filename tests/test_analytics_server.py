import sqlite3
import pytest
from analytics.server import _query_decision


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
            hold_hours INTEGER DEFAULT 0
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
    assert result["ticker"] == "JPM"
    assert result["confidence"] == pytest.approx(0.85)
    assert result["reasoning"] == "Banks benefit from rate hikes"
    assert result["hold_hours"] == 2
