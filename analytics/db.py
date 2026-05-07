# analytics/db.py

import logging
import sqlite3
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


class TradeDB:
    def __init__(self, path: str) -> None:
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 30000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._create_tables()

    def _create_tables(self) -> None:
        with self._lock:
            self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS news_events (
                id       INTEGER PRIMARY KEY AUTOINCREMENT,
                ts       TEXT NOT NULL,
                headline TEXT NOT NULL,
                summary  TEXT,
                symbols  TEXT
            );
            CREATE TABLE IF NOT EXISTS llm_decisions (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                news_event_id INTEGER REFERENCES news_events(id),
                ts            TEXT NOT NULL,
                action        TEXT NOT NULL,
                ticker        TEXT,
                reasoning     TEXT,
                confidence    REAL DEFAULT 0.0,
                hold_hours    INTEGER DEFAULT 0,
                is_primary    INTEGER DEFAULT 0,
                skip_reason   TEXT
            );
            CREATE TABLE IF NOT EXISTS trades (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id       INTEGER REFERENCES llm_decisions(id),
                ticker            TEXT NOT NULL,
                side              TEXT NOT NULL,
                qty               INTEGER NOT NULL,
                entry_price       REAL,
                exit_price        REAL,
                pnl_usd           REAL,
                pnl_pct           REAL,
                exit_reason       TEXT,
                fill_latency_sec  REAL,
                hold_hours        INTEGER DEFAULT 0,
                bracket_order_id  TEXT,
                opened_at         TEXT NOT NULL,
                closed_at         TEXT
            );
        """)
            # Migrations for columns added after initial schema
            for ddl in [
                "ALTER TABLE trades ADD COLUMN fill_latency_sec REAL",
                "ALTER TABLE llm_decisions ADD COLUMN confidence REAL DEFAULT 0.0",
                "ALTER TABLE llm_decisions ADD COLUMN hold_hours INTEGER DEFAULT 0",
                "ALTER TABLE trades ADD COLUMN hold_hours INTEGER DEFAULT 0",
                "ALTER TABLE llm_decisions ADD COLUMN provider TEXT",
                "ALTER TABLE llm_decisions ADD COLUMN latency_sec REAL",
                "ALTER TABLE llm_decisions ADD COLUMN cost_usd REAL",
                "ALTER TABLE llm_decisions ADD COLUMN is_primary INTEGER DEFAULT 0",
                "ALTER TABLE llm_decisions ADD COLUMN skip_reason TEXT",
                "ALTER TABLE trades ADD COLUMN bracket_order_id TEXT",
            ]:
                try:
                    self._conn.execute(ddl)
                except sqlite3.OperationalError:
                    pass  # column already exists
            self._conn.commit()

    def record_news(self, ts: str, headline: str, summary: str | None, symbols: list[str]) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO news_events (ts, headline, summary, symbols) VALUES (?, ?, ?, ?)",
                (ts, headline, summary, ",".join(symbols) if symbols else ""),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def record_decision(
        self,
        news_event_id: int | None,
        ts: str,
        action: str,
        ticker: str | None,
        reasoning: str,
        confidence: float = 0.0,
        hold_hours: int = 0,
        provider: str | None = None,
        latency_sec: float | None = None,
        cost_usd: float | None = None,
        is_primary: bool = False,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO llm_decisions "
                "(news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec, cost_usd, is_primary) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    news_event_id, ts, action, ticker, reasoning, confidence, hold_hours,
                    provider, latency_sec, cost_usd, 1 if is_primary else 0,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def record_skip(self, decision_id: int, reason: str) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE llm_decisions SET skip_reason=? WHERE id=?",
                (reason, decision_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            logger.warning("record_skip: no decision row found for id=%s", decision_id)
            return False
        return True

    def record_trade_open(
        self,
        decision_id: int | None,
        ticker: str,
        side: str,
        qty: int,
        entry_price: float | None,
        opened_at: str,
        fill_latency_sec: float | None = None,
        hold_hours: int = 0,
        bracket_order_id: str | None = None,
    ) -> int:
        with self._lock:
            cur = self._conn.execute(
                "INSERT INTO trades "
                "(decision_id, ticker, side, qty, entry_price, opened_at, fill_latency_sec, hold_hours, bracket_order_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id, ticker, side, qty, entry_price, opened_at,
                    fill_latency_sec, hold_hours, bracket_order_id,
                ),
            )
            self._conn.commit()
            return cur.lastrowid  # type: ignore[return-value]

    def record_trade_close(
        self,
        trade_id: int,
        exit_price: float | None,
        pnl_usd: float | None,
        pnl_pct: float | None,
        exit_reason: str,
        closed_at: str,
    ) -> bool:
        with self._lock:
            cur = self._conn.execute(
                "UPDATE trades SET exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=?, closed_at=? "
                "WHERE id=? AND closed_at IS NULL",
                (exit_price, pnl_usd, pnl_pct, exit_reason, closed_at, trade_id),
            )
            self._conn.commit()
        if cur.rowcount == 0:
            logger.warning("record_trade_close: no open trade row found for id=%s", trade_id)
            return False
        return True

    def get_open_trades(self) -> list[dict]:
        """Return all trades with no closed_at (i.e. positions still open per DB)."""
        with self._lock:
            cur = self._conn.execute(
                "SELECT id, ticker, side, qty, entry_price, hold_hours, opened_at, bracket_order_id "
                "FROM trades WHERE closed_at IS NULL"
            )
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, row)) for row in cur.fetchall()]

    def realized_summary_for_et_date(self, target_date) -> tuple[int, int, float]:
        """Return successful entries, realized exits, and realized P&L for one ET date."""
        et = ZoneInfo("America/New_York")
        with self._lock:
            rows = self._conn.execute(
                "SELECT opened_at, closed_at, pnl_usd FROM trades"
            ).fetchall()
        buys = 0
        sells = 0
        pnl = 0.0
        for opened_at_raw, closed_at_raw, pnl_usd in rows:
            opened_at = _parse_iso_dt(opened_at_raw)
            if opened_at is not None and opened_at.astimezone(et).date() == target_date:
                buys += 1
            closed_at = _parse_iso_dt(closed_at_raw)
            if (
                closed_at is not None
                and closed_at.astimezone(et).date() == target_date
                and pnl_usd is not None
            ):
                sells += 1
                pnl += float(pnl_usd)
        return buys, sells, pnl

    def realized_summary_for_et_week(self, week_monday) -> tuple[int, int, float]:
        """Return successful entries, realized exits, and realized P&L for one ET week."""
        et = ZoneInfo("America/New_York")
        with self._lock:
            rows = self._conn.execute(
                "SELECT opened_at, closed_at, pnl_usd FROM trades"
            ).fetchall()
        buys = 0
        sells = 0
        pnl = 0.0
        for opened_at_raw, closed_at_raw, pnl_usd in rows:
            opened_at = _parse_iso_dt(opened_at_raw)
            if opened_at is not None:
                opened_day = opened_at.astimezone(et).date()
                if opened_day - timedelta(days=opened_day.weekday()) == week_monday:
                    buys += 1
            closed_at = _parse_iso_dt(closed_at_raw)
            if closed_at is not None and pnl_usd is not None:
                closed_day = closed_at.astimezone(et).date()
                if closed_day - timedelta(days=closed_day.weekday()) == week_monday:
                    sells += 1
                    pnl += float(pnl_usd)
        return buys, sells, pnl

    def close(self) -> None:
        with self._lock:
            self._conn.close()
