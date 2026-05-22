import sqlite3
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from config import load_config
from main import _make_tradier_client


def _scalar(con: sqlite3.Connection, sql: str, params: tuple = ()):
    return con.execute(sql, params).fetchone()[0]


def _et_window(day):
    et = ZoneInfo("America/New_York")
    start = datetime(day.year, day.month, day.day, tzinfo=et).astimezone(timezone.utc)
    end_day = day + timedelta(days=1)
    end = datetime(end_day.year, end_day.month, end_day.day, tzinfo=et).astimezone(timezone.utc)
    return start.isoformat(), end.isoformat()


def main() -> None:
    cfg = load_config()
    print("config", {
        "paper": cfg.tradier_paper,
        "trade_amount_usd": cfg.trade_amount_usd,
        "allow_short": cfg.allow_short,
        "min_confidence": cfg.min_confidence,
        "min_trade_price": cfg.min_trade_price,
        "max_entry_spread_pct": cfg.max_entry_spread_pct,
        "min_entry_avg_volume": cfg.min_entry_avg_volume,
        "min_entry_avg_dollar_volume": cfg.min_entry_avg_dollar_volume,
        "default_hold_hours": cfg.default_hold_hours,
        "max_hold_hours": cfg.max_hold_hours,
        "close_before_market_close_minutes": cfg.close_before_market_close_minutes,
        "require_hard_catalyst_news": cfg.require_hard_catalyst_news,
        "block_soft_partnership_news": cfg.block_soft_partnership_news,
        "short_liquid_only": cfg.short_liquid_only,
        "entry_confirmation_enabled": cfg.entry_confirmation_enabled,
        "entry_confirmation_lookback_minutes": cfg.entry_confirmation_lookback_minutes,
        "entry_confirmation_trend_minutes": cfg.entry_confirmation_trend_minutes,
        "feed": cfg.alpaca_data_feed,
        "analytics_db_path": cfg.analytics_db_path,
    })

    con = sqlite3.connect(cfg.analytics_db_path)
    con.row_factory = sqlite3.Row
    try:
        print("table_counts", {
            "news_events": _scalar(con, "SELECT COUNT(*) FROM news_events"),
            "llm_decisions": _scalar(con, "SELECT COUNT(*) FROM llm_decisions"),
            "trades": _scalar(con, "SELECT COUNT(*) FROM trades"),
            "account_value_snapshots": _scalar(con, "SELECT COUNT(*) FROM account_value_snapshots"),
        })
        print("latest_ts", {
            "news": _scalar(con, "SELECT MAX(ts) FROM news_events"),
            "decision": _scalar(con, "SELECT MAX(ts) FROM llm_decisions"),
            "trade_opened": _scalar(con, "SELECT MAX(opened_at) FROM trades"),
            "trade_closed": _scalar(con, "SELECT MAX(closed_at) FROM trades"),
            "account_value": _scalar(con, "SELECT MAX(ts) FROM account_value_snapshots"),
        })

        et = ZoneInfo("America/New_York")
        today_et = datetime.now(et).date()
        days = [today_et - timedelta(days=2), today_et - timedelta(days=1), today_et]
        for day in days:
            start, end = _et_window(day)
            decisions = con.execute(
                "SELECT action, COUNT(*) cnt FROM llm_decisions WHERE ts >= ? AND ts < ? GROUP BY action",
                (start, end),
            ).fetchall()
            skips = con.execute(
                "SELECT skip_reason, COUNT(*) cnt FROM llm_decisions "
                "WHERE ts >= ? AND ts < ? AND skip_reason IS NOT NULL "
                "GROUP BY skip_reason ORDER BY cnt DESC",
                (start, end),
            ).fetchall()
            trades = con.execute(
                "SELECT ticker, side, opened_at, closed_at, pnl_usd, exit_reason "
                "FROM trades WHERE opened_at >= ? AND opened_at < ? ORDER BY opened_at",
                (start, end),
            ).fetchall()
            acct = con.execute(
                "SELECT ts, value_usd FROM account_value_snapshots WHERE ts >= ? AND ts < ? ORDER BY ts",
                (start, end),
            ).fetchall()
            acct_summary = None
            if acct:
                acct_summary = {
                    "first": (acct[0]["ts"], acct[0]["value_usd"]),
                    "last": (acct[-1]["ts"], acct[-1]["value_usd"]),
                    "delta": acct[-1]["value_usd"] - acct[0]["value_usd"],
                }
            print("day", day.isoformat(), {
                "decisions": {r["action"]: r["cnt"] for r in decisions},
                "skip_reasons": {r["skip_reason"]: r["cnt"] for r in skips},
                "trades": [dict(r) for r in trades],
                "account": acct_summary,
            })
            candidates = con.execute(
                "SELECT d.ts, d.action, d.ticker, d.confidence, d.skip_reason, "
                "       n.headline, n.symbols, substr(d.reasoning, 1, 220) AS reasoning "
                "FROM llm_decisions d "
                "LEFT JOIN news_events n ON n.id = d.news_event_id "
                "WHERE d.ts >= ? AND d.ts < ? AND lower(d.action) IN ('buy', 'short') "
                "ORDER BY d.ts",
                (start, end),
            ).fetchall()
            if candidates:
                print("trade_candidates", day.isoformat())
                for row in candidates:
                    print(dict(row))

        recent = con.execute(
            "SELECT ts, action, ticker, confidence, skip_reason, reasoning "
            "FROM llm_decisions ORDER BY id DESC LIMIT 20"
        ).fetchall()
        print("recent_decisions")
        for row in recent:
            print(dict(row))
    finally:
        con.close()

    client = _make_tradier_client(cfg)
    try:
        print("broker_clock_open", client.get_clock().is_open)
        print("broker_positions", [(p.symbol, p.qty, p.cost_basis) for p in client.get_all_positions()])
        for day in days:
            try:
                print("broker_activity_summary", day.isoformat(), client.trade_activity_summary_for_date(day))
            except Exception as exc:
                print("broker_activity_summary_error", day.isoformat(), type(exc).__name__, str(exc)[:200])
        try:
            orders = client.get_account_orders()
            print("broker_orders_count", len(orders))
            for order in orders[:20]:
                print(
                    "broker_order",
                    {
                        "symbol": order.symbol,
                        "side": order.side,
                        "status": order.status,
                        "type": order.order_type,
                        "avg_fill_price": order.avg_fill_price,
                        "filled_at": order.filled_at,
                        "quantity": order.quantity,
                        "order_id": order.order_id,
                    },
                )
        except Exception as exc:
            print("broker_orders_error", type(exc).__name__, str(exc)[:300])
    finally:
        client.close()


if __name__ == "__main__":
    main()
