"""
Export trades.db to stdout in markdown format — paste directly into any LLM client.

Usage:
    python export_db.py              # uses default DB path (data/trades.db)
    python export_db.py path/to.db   # custom path
"""

import sqlite3
import sys


def _md_table(cur: sqlite3.Cursor) -> str:
    rows = cur.fetchall()
    if not rows:
        return "_no rows_\n"
    cols = [d[0] for d in cur.description]
    sep = "| " + " | ".join("---" for _ in cols) + " |"
    header = "| " + " | ".join(cols) + " |"
    lines = [header, sep]
    for row in rows:
        cells = [str(v) if v is not None else "" for v in row]
        lines.append("| " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def export(db_path: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    sections = [
        (
            "## Trades",
            """
            SELECT t.id, t.ticker, t.side, t.qty,
                   t.entry_price, t.exit_price,
                   ROUND(t.pnl_usd, 2)  AS pnl_usd,
                   ROUND(t.pnl_pct * 100, 2) AS pnl_pct,
                   t.exit_reason, t.hold_hours,
                   ROUND(t.fill_latency_sec, 2) AS fill_latency_sec,
                   t.opened_at, t.closed_at
            FROM trades t
            ORDER BY t.opened_at DESC
            """,
        ),
        (
            "## LLM Decisions",
            """
            SELECT d.id, d.ts, d.action, d.ticker,
                   ROUND(d.confidence, 2) AS confidence,
                   d.hold_hours, d.reasoning
            FROM llm_decisions d
            ORDER BY d.ts DESC
            LIMIT 100
            """,
        ),
        (
            "## News Events",
            """
            SELECT id, ts, headline, symbols
            FROM news_events
            ORDER BY ts DESC
            LIMIT 50
            """,
        ),
    ]

    # Apply any pending migrations so the script works against older DBs
    for ddl in [
        "ALTER TABLE trades ADD COLUMN fill_latency_sec REAL",
        "ALTER TABLE llm_decisions ADD COLUMN confidence REAL DEFAULT 0.0",
        "ALTER TABLE llm_decisions ADD COLUMN hold_hours INTEGER DEFAULT 0",
        "ALTER TABLE trades ADD COLUMN hold_hours INTEGER DEFAULT 0",
    ]:
        try:
            conn.execute(ddl)
            conn.commit()
        except sqlite3.OperationalError:
            pass

    print(f"# Trading Bot DB Export\n")
    print(f"**Source:** `{db_path}`\n")

    for heading, sql in sections:
        print(heading)
        cur = conn.execute(sql)
        print(_md_table(cur))

    conn.close()


if __name__ == "__main__":
    path = sys.argv[1] if len(sys.argv) > 1 else "data/trades.db"
    export(path)
