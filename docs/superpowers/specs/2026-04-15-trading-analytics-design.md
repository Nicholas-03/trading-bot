# Trading Analytics — Design Spec

**Date:** 2026-04-15
**Status:** Approved

## Goal

Persist every trading event (news → LLM decision → trade open → trade close) to SQLite so the full chain can be queried and visualized on demand. A standalone FastAPI + Plotly web UI provides six interactive charts for parameter fine-tuning (SL/TP thresholds) and trade debugging.

---

## Database Schema

**File:** `data/trades.db` (SQLite). Created automatically on first bot run. Gitignored; `data/.gitkeep` tracks the directory.

### `news_events`

| column    | type        | notes                        |
|-----------|-------------|------------------------------|
| `id`      | INTEGER PK  | autoincrement                |
| `ts`      | TEXT        | ISO 8601 UTC timestamp       |
| `headline`| TEXT        |                              |
| `summary` | TEXT        |                              |
| `symbols` | TEXT        | comma-separated ticker list  |

### `llm_decisions`

| column          | type        | notes                              |
|-----------------|-------------|------------------------------------|
| `id`            | INTEGER PK  | autoincrement                      |
| `news_event_id` | INTEGER FK  | → `news_events.id`                 |
| `ts`            | TEXT        | ISO 8601 UTC timestamp             |
| `action`        | TEXT        | `buy` / `sell` / `short` / `hold` |
| `ticker`        | TEXT        | nullable                           |
| `reasoning`     | TEXT        |                                    |

### `trades`

| column        | type        | notes                                              |
|---------------|-------------|----------------------------------------------------|
| `id`          | INTEGER PK  | autoincrement                                      |
| `decision_id` | INTEGER FK  | → `llm_decisions.id`; nullable (SL/TP exits)      |
| `ticker`      | TEXT        |                                                    |
| `side`        | TEXT        | `buy` / `sell` / `short` / `cover`                |
| `qty`         | INTEGER     |                                                    |
| `entry_price` | REAL        | nullable; set at open                              |
| `exit_price`  | REAL        | nullable; set at close                             |
| `pnl_usd`     | REAL        | nullable                                           |
| `pnl_pct`     | REAL        | nullable                                           |
| `exit_reason` | TEXT        | `llm` / `stop_loss` / `take_profit`; nullable      |
| `opened_at`   | TEXT        | ISO 8601 UTC timestamp                             |
| `closed_at`   | TEXT        | ISO 8601 UTC timestamp; nullable until position closed |

---

## Data Collection Layer

### `analytics/db.py` — `TradeDB` class

Wraps `sqlite3`. Initialised with a path (or `:memory:` for tests). Runs `CREATE TABLE IF NOT EXISTS` on `__init__`. All public methods are synchronous (callers wrap in `asyncio.to_thread`).

**Methods:**

```python
def record_news(self, ts, headline, summary, symbols: list[str]) -> int
    # Returns news_event_id

def record_decision(self, news_event_id, ts, action, ticker, reasoning) -> int
    # Returns decision_id

def record_trade_open(self, decision_id, ticker, side, qty, entry_price, opened_at) -> int
    # Returns trade_id

def record_trade_close(self, trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at) -> None
```

### Wiring into existing components

**`OrderExecutor`:**
- Gains an optional `db: TradeDB | None = None` constructor parameter.
- `_position_book` value changes from `(float, int)` to `(float, int, int)` — adds `trade_id` as third element.
- `buy()` calls `db.record_trade_open()` after a successful order; stores `trade_id` in `_position_book`.
- `short()` calls `db.record_trade_open()` with `entry_price=None` (unknown at short time).
- `sell()` calls `db.record_trade_close()` with the computed exit price, P&L, and exit reason.
- When `db is None`, all recording calls are skipped — existing tests remain unbroken.

**`NewsHandler._handle_news()`:**
- After calling `asyncio.to_thread(self._client.get_clock)` and confirming market open, records the news event.
- After `self._advisor.analyze()` returns, records the LLM decision.
- Passes `decision_id` into `executor.buy()` / `executor.short()` / `executor.sell()`.
- `OrderExecutor.buy()` / `short()` / `sell()` signatures gain an optional `decision_id: int | None = None`.

**`PositionMonitor._check_positions()`:**
- Passes `exit_reason="stop_loss"` or `exit_reason="take_profit"` to `executor.sell()`.
- For monitor-triggered sells, `decision_id` is `None`.

---

## Analytics Server

**File:** `analytics/server.py`

Run standalone: `python -m analytics.server` (defaults to port 8080).

**Single route:** `GET /` — queries `data/trades.db`, builds all six Plotly figures, serializes them to JSON, and returns a self-contained HTML page with Plotly.js loaded from CDN. No caching — always reads fresh data.

### Charts

1. **Cumulative P&L curve** — line chart; x = `closed_at` date, y = running sum of `pnl_usd` across all closed trades.
2. **Daily P&L bars** — bar chart; one bar per calendar day, summing `pnl_usd`.
3. **SL/TP hit rate** — donut chart; segments for `stop_loss`, `take_profit`, `llm` exit reasons.
4. **P&L distribution at exit** — histogram of `pnl_pct * 100` (%) across all closed trades; vertical lines marking configured SL/TP thresholds.
5. **Trade duration** — histogram of `(closed_at - opened_at)` in minutes across all closed trades.
6. **LLM decision breakdown** — two sub-charts: (a) bar chart of action counts (buy/sell/short/hold); (b) scrollable HTML table of the 20 most recent rows joining `news_events → llm_decisions → trades`, showing headline, action, ticker, P&L outcome.

---

## File Layout

```
analytics/
  __init__.py
  db.py           # TradeDB class
  server.py       # FastAPI app + chart generation
data/
  .gitkeep        # directory tracked; trades.db gitignored
```

**`.gitignore` additions:**
```
data/trades.db
```

**`requirements.txt` additions:**
```
fastapi
uvicorn[standard]
plotly
```

---

## Testing

**`tests/test_analytics_db.py`** — uses `TradeDB(":memory:")`.

Covers:
- `record_news` returns an integer ID and row is queryable.
- `record_decision` links correctly to `news_event_id`.
- `record_trade_open` links correctly to `decision_id`; `decision_id=None` is accepted.
- `record_trade_close` updates the correct row with exit price, P&L, reason, and timestamp.
- Full chain: news → decision → open → close, verifying all FK links.
- `db=None` path in `OrderExecutor` — buy/sell complete without error when no DB is wired.

No tests for `server.py` (thin query + render layer).

---

## Usage

```bash
# Start the bot (recording enabled automatically when db path is set in .env)
python main.py

# View analytics (separate terminal, on demand)
python -m analytics.server
# Open http://localhost:8080
```

A new optional env var `ANALYTICS_DB_PATH` (default: `data/trades.db`) controls the SQLite file location. When absent or empty, recording is disabled.
