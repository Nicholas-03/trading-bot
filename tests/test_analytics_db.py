from concurrent.futures import ThreadPoolExecutor

import pytest
from datetime import date
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


def test_record_trade_open_stores_bracket_order_id(db):
    tid = db.record_trade_open(
        None,
        "AAPL",
        "buy",
        1,
        100.0,
        "2026-01-01T00:00:00Z",
        bracket_order_id="otoco-123",
    )
    row = db._conn.execute("SELECT bracket_order_id FROM trades WHERE id=?", (tid,)).fetchone()
    assert row[0] == "otoco-123"


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
        provider="chatgpt", latency_sec=1.23,
    )
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] == "chatgpt"
    assert abs(row[1] - 1.23) < 0.001


def test_record_decision_provider_defaults_to_none(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-01-01T00:00:01Z", "hold", None, "reason")
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] is None
    assert row[1] is None


def test_record_decision_stores_cost_usd(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(
        nid, "2026-01-01T00:00:01Z", "buy", "AAPL", "reason", 0.9, 2,
        provider="chatgpt", latency_sec=1.23, cost_usd=0.0035,
    )
    row = db._conn.execute(
        "SELECT cost_usd FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert abs(row[0] - 0.0035) < 1e-9


def test_record_decision_stores_primary_marker(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(
        nid, "2026-01-01T00:00:01Z", "buy", "AAPL", "reason",
        provider="chatgpt", is_primary=True,
    )
    row = db._conn.execute("SELECT is_primary FROM llm_decisions WHERE id=?", (did,)).fetchone()
    assert row[0] == 1


def test_record_decision_cost_defaults_to_none(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-01-01T00:00:01Z", "hold", None, "reason")
    row = db._conn.execute(
        "SELECT cost_usd FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] is None


def test_realized_summary_for_et_date_counts_real_fills_only(db):
    db.record_trade_open(None, "SONY", "buy", 2, 20.56, "2026-05-06T15:51:08Z")
    ztek = db.record_trade_open(None, "ZTEK", "buy", 89, 0.51, "2026-05-06T16:33:27Z")
    din = db.record_trade_open(None, "DIN", "buy", 1, 28.74, "2026-05-06T17:59:59Z")
    unknown = db.record_trade_open(None, "BLSH", "buy", 1, 47.78, "2026-05-05T14:06:19Z")
    db.record_trade_close(ztek, 0.49, -1.78, -0.039, "stop_loss", "2026-05-06T16:33:27Z")
    db.record_trade_close(din, 26.50, -2.24, -0.078, "stop_loss", "2026-05-06T19:53:36Z")
    db.record_trade_close(unknown, None, None, None, "offline_close", "2026-05-06T19:54:00Z")

    buys, sells, pnl = db.realized_summary_for_et_date(date(2026, 5, 6))

    assert buys == 3
    assert sells == 2
    assert abs(pnl - (-4.02)) < 1e-9


def test_record_skip_returns_false_for_missing_decision(db):
    assert db.record_skip(999, "buy_exception") is False


def test_record_trade_close_does_not_overwrite_closed_trade(db):
    tid = db.record_trade_open(None, "AAPL", "buy", 1, 100.0, "2026-01-01T00:00:00Z")
    assert db.record_trade_close(tid, 103.0, 3.0, 0.03, "take_profit", "2026-01-01T01:00:00Z")
    assert not db.record_trade_close(tid, 98.0, -2.0, -0.02, "stop_loss", "2026-01-01T02:00:00Z")
    row = db._conn.execute(
        "SELECT exit_price, pnl_usd, pnl_pct, exit_reason, closed_at FROM trades WHERE id=?",
        (tid,),
    ).fetchone()
    assert row[0] == 103.0
    assert row[1] == 3.0
    assert row[2] == 0.03
    assert row[3] == "take_profit"
    assert row[4] == "2026-01-01T01:00:00Z"


def test_db_serializes_concurrent_writes(db):
    def write_one(i: int) -> int:
        return db.record_news(f"2026-01-01T00:00:{i:02d}Z", f"headline {i}", None, ["AAPL"])

    with ThreadPoolExecutor(max_workers=8) as pool:
        ids = list(pool.map(write_one, range(20)))

    count = db._conn.execute("SELECT COUNT(*) FROM news_events").fetchone()[0]
    assert len(set(ids)) == 20
    assert count == 20
