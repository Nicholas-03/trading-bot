# analytics/db.py

import logging
import sqlite3

logger = logging.getLogger(__name__)


class TradeDB:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._create_tables()

    def _create_tables(self) -> None:
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
            "ALTER TABLE llm_decisions ADD COLUMN skip_reason TEXT",
        ]:
            try:
                self._conn.execute(ddl)
                self._conn.commit()
            except sqlite3.OperationalError:
                pass  # column already exists

    def record_news(self, ts: str, headline: str, summary: str | None, symbols: list[str]) -> int:
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
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO llm_decisions "
            "(news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec, cost_usd) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec, cost_usd),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_skip(self, decision_id: int, reason: str) -> None:
        self._conn.execute(
            "UPDATE llm_decisions SET skip_reason=? WHERE id=?",
            (reason, decision_id),
        )
        self._conn.commit()

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
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (decision_id, ticker, side, qty, entry_price, opened_at, fill_latency_sec, hold_hours) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (decision_id, ticker, side, qty, entry_price, opened_at, fill_latency_sec, hold_hours),
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
    ) -> None:
        cur = self._conn.execute(
            "UPDATE trades SET exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl_usd, pnl_pct, exit_reason, closed_at, trade_id),
        )
        self._conn.commit()
        if cur.rowcount == 0:
            logger.warning("record_trade_close: no trade row found for id=%s", trade_id)

    def get_open_trades(self) -> list[dict]:
        """Return all trades with no closed_at (i.e. positions still open per DB)."""
        cur = self._conn.execute(
            "SELECT id, ticker, side, qty, entry_price, hold_hours, opened_at "
            "FROM trades WHERE closed_at IS NULL"
        )
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()
