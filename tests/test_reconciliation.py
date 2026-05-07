from unittest.mock import MagicMock

from analytics.db import TradeDB
from main import _reconcile_stale_trades
from trading.tradier_client import TradierOrder


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
