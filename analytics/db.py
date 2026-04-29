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
                reasoning     TEXT
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
                opened_at         TEXT NOT NULL,
                closed_at         TEXT
            );
        """)
        # Migration: add fill_latency_sec to existing databases that predate this column
        try:
            self._conn.execute("ALTER TABLE trades ADD COLUMN fill_latency_sec REAL")
            self._conn.commit()
        except Exception:
            pass  # column already exists

    def record_news(self, ts: str, headline: str, summary: str | None, symbols: list[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO news_events (ts, headline, summary, symbols) VALUES (?, ?, ?, ?)",
            (ts, headline, summary, ",".join(symbols) if symbols else ""),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_decision(
        self, news_event_id: int | None, ts: str, action: str, ticker: str | None, reasoning: str
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO llm_decisions (news_event_id, ts, action, ticker, reasoning) VALUES (?, ?, ?, ?, ?)",
            (news_event_id, ts, action, ticker, reasoning),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_trade_open(
        self,
        decision_id: int | None,
        ticker: str,
        side: str,
        qty: int,
        entry_price: float | None,
        opened_at: str,
        fill_latency_sec: float | None = None,
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (decision_id, ticker, side, qty, entry_price, opened_at, fill_latency_sec) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (decision_id, ticker, side, qty, entry_price, opened_at, fill_latency_sec),
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

    def close(self) -> None:
        self._conn.close()
