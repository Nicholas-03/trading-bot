import pytest
from analytics.db import TradeDB


@pytest.fixture
def db():
    return TradeDB(":memory:")


def test_record_news_returns_id(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "AAPL beats earnings", "Summary", ["AAPL", "MSFT"])
    assert isinstance(nid, int)
    assert nid > 0


def test_record_news_stores_symbols_as_csv(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "headline", None, ["AAPL", "TSLA"])
    row = db._conn.execute("SELECT symbols FROM news_events WHERE id=?", (nid,)).fetchone()
    assert row[0] == "AAPL,TSLA"


def test_record_news_empty_symbols(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "headline", None, [])
    row = db._conn.execute("SELECT symbols FROM news_events WHERE id=?", (nid,)).fetchone()
    assert row[0] == ""


def test_record_decision_links_to_news(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-04-15T10:00:01Z", "buy", "AAPL", "Bullish earnings")
    row = db._conn.execute("SELECT news_event_id, action, ticker FROM llm_decisions WHERE id=?", (did,)).fetchone()
    assert row[0] == nid
    assert row[1] == "buy"
    assert row[2] == "AAPL"


def test_record_trade_open_with_decision(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-04-15T10:00:01Z", "buy", "AAPL", "reason")
    tid = db.record_trade_open(did, "AAPL", "buy", 3, 150.0, "2026-04-15T10:00:02Z", fill_latency_sec=4.2)
    row = db._conn.execute(
        "SELECT decision_id, ticker, side, qty, entry_price, closed_at, fill_latency_sec FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert row[0] == did
    assert row[1] == "AAPL"
    assert row[2] == "buy"
    assert row[3] == 3
    assert abs(row[4] - 150.0) < 0.001
    assert row[5] is None
    assert abs(row[6] - 4.2) < 0.001


def test_record_trade_open_without_decision(db):
    tid = db.record_trade_open(None, "TSLA", "short", 1, None, "2026-04-15T10:00:00Z")
    row = db._conn.execute("SELECT decision_id, entry_price, fill_latency_sec FROM trades WHERE id=?", (tid,)).fetchone()
    assert row[0] is None
    assert row[1] is None
    assert row[2] is None


def test_record_trade_close_updates_row(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-04-15T10:00:01Z", "buy", "AAPL", "reason")
    tid = db.record_trade_open(did, "AAPL", "buy", 2, 100.0, "2026-04-15T10:00:02Z")
    db.record_trade_close(tid, 110.0, 20.0, 0.10, "stop_loss", "2026-04-15T14:00:00Z")
    row = db._conn.execute(
        "SELECT exit_price, pnl_usd, pnl_pct, exit_reason, closed_at FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert abs(row[0] - 110.0) < 0.001
    assert abs(row[1] - 20.0) < 0.001
    assert abs(row[2] - 0.10) < 0.001
    assert row[3] == "stop_loss"
    assert row[4] == "2026-04-15T14:00:00Z"


def test_full_chain(db):
    nid = db.record_news("2026-04-15T10:00:00Z", "TSLA recall", "Details", ["TSLA"])
    did = db.record_decision(nid, "2026-04-15T10:00:01Z", "sell", "TSLA", "Bearish recall")
    tid = db.record_trade_open(did, "TSLA", "sell", 2, 250.0, "2026-04-15T10:00:02Z")
    db.record_trade_close(tid, 240.0, -20.0, -0.04, "llm", "2026-04-15T11:00:00Z")

    decision = db._conn.execute("SELECT news_event_id FROM llm_decisions WHERE id=?", (did,)).fetchone()
    assert decision[0] == nid

    trade = db._conn.execute("SELECT decision_id, exit_reason FROM trades WHERE id=?", (tid,)).fetchone()
    assert trade[0] == did
    assert trade[1] == "llm"


def test_record_decision_stores_provider_and_latency(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(
        nid, "2026-01-01T00:00:01Z", "buy", "AAPL", "reason", 0.9, 2,
        provider="claude", latency_sec=1.23,
    )
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] == "claude"
    assert abs(row[1] - 1.23) < 0.001


def test_record_decision_provider_defaults_to_none(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-01-01T00:00:01Z", "hold", None, "reason")
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
