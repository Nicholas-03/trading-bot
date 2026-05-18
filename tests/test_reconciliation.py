from unittest.mock import MagicMock

from analytics.db import TradeDB
from main import _adopt_live_positions_missing_from_db, _reconcile_stale_trades
from trading.tradier_client import TradierOrder, TradierPosition


def test_reconcile_stale_trade_uses_close_fill_after_open(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        trade_id = db.record_trade_open(
            None, "AAPL", "buy", 2, 100.0, "2026-05-01T14:00:00Z"
        )
        client = MagicMock()
        client.get_account_orders.return_value = [
            TradierOrder("AAPL", "sell", "filled", "limit", 101.0, "2026-04-30T15:00:00Z", 2),
            TradierOrder("AAPL", "sell", "filled", "stop", 98.0, "2026-05-01T15:00:00Z", 2),
            TradierOrder("AAPL", "sell", "filled", "limit", 106.0, "2026-05-01T18:00:00Z", 2),
        ]

        _reconcile_stale_trades(client, db, db.get_open_trades())

        row = db._conn.execute(
            "SELECT exit_price, pnl_usd, pnl_pct, exit_reason, closed_at FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        assert row[0] == 98.0
        assert row[1] == -4.0
        assert row[2] == -0.02
        assert row[3] == "stop_loss"
        assert row[4] == "2026-05-01T15:00:00Z"
    finally:
        db.close()


def test_reconcile_stale_trade_marks_unknown_when_history_missing(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        trade_id = db.record_trade_open(
            None, "MSFT", "buy", 1, 100.0, "2026-05-01T14:00:00Z"
        )
        client = MagicMock()
        client.get_account_orders.return_value = []

        _reconcile_stale_trades(client, db, db.get_open_trades())

        row = db._conn.execute(
            "SELECT exit_price, pnl_usd, exit_reason, closed_at FROM trades WHERE id=?",
            (trade_id,),
        ).fetchone()
        assert row[0] is None
        assert row[1] is None
        assert row[2] == "reconciled_unknown_exit"
        assert row[3] is not None
    finally:
        db.close()


def test_adopt_live_position_missing_from_db_links_recent_decision(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        decision_id = db.record_decision(
            None,
            "2026-05-18T19:33:01Z",
            "buy",
            "SNY",
            "phase 2 catalyst",
            hold_hours=3,
        )
        client = MagicMock()
        client.get_all_positions.return_value = [
            TradierPosition("SNY", 11.0, 476.74),
        ]
        client.get_account_orders.return_value = [
            TradierOrder("SNY", "buy", "filled", "limit", 43.34, "2026-05-18T19:35:33Z", 11),
        ]

        open_trades = _adopt_live_positions_missing_from_db(
            client, db, [], {"SNY"}, set()
        )

        assert len(open_trades) == 1
        row = db._conn.execute(
            "SELECT decision_id, ticker, side, qty, entry_price, opened_at, hold_hours FROM trades"
        ).fetchone()
        assert row == (
            decision_id,
            "SNY",
            "buy",
            11,
            43.34,
            "2026-05-18T19:35:33Z",
            3,
        )
    finally:
        db.close()


def test_adopt_live_position_missing_from_db_skips_existing_open_trade(tmp_path):
    db = TradeDB(str(tmp_path / "trades.db"))
    try:
        trade_id = db.record_trade_open(None, "BSX", "buy", 8, 55.57, "2026-05-18T18:40:57Z")
        client = MagicMock()
        client.get_all_positions.return_value = [
            TradierPosition("BSX", 8.0, 444.56),
        ]

        open_trades = _adopt_live_positions_missing_from_db(
            client, db, db.get_open_trades(), {"BSX"}, set()
        )

        assert [t["id"] for t in open_trades] == [trade_id]
        assert db._conn.execute("SELECT COUNT(*) FROM trades").fetchone()[0] == 1
        client.get_all_positions.assert_not_called()
    finally:
        db.close()
