# Trading Analytics Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist every news → LLM decision → trade open → trade close event to SQLite and serve six interactive Plotly charts via a standalone FastAPI web UI.

**Architecture:** A new `analytics/` module provides `TradeDB` (SQLite wrapper) and `server.py` (FastAPI app). `OrderExecutor`, `NewsHandler`, and `PositionMonitor` gain optional DB write calls at each trade lifecycle event. The analytics server is run separately on demand — it has no coupling to the bot runtime.

**Tech Stack:** Python stdlib `sqlite3`, FastAPI, uvicorn, Plotly

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `analytics/__init__.py` | Package marker |
| Create | `analytics/db.py` | `TradeDB` — schema creation + 4 write methods |
| Create | `analytics/server.py` | FastAPI app — query DB, render 6 Plotly charts |
| Create | `data/.gitkeep` | Track `data/` dir in git |
| Create | `tests/test_analytics_db.py` | Unit tests for `TradeDB` against `:memory:` |
| Modify | `requirements.txt` | Add `fastapi`, `uvicorn[standard]`, `plotly` |
| Modify | `.gitignore` | Add `data/trades.db` |
| Modify | `config.py` | Add `analytics_db_path: str` field |
| Modify | `trading/order_executor.py` | Add `db` param; update `_position_book` tuple; record open/close |
| Modify | `tests/test_order_executor.py` | Update `_position_book` tuples to 3-element form |
| Modify | `news/news_handler.py` | Add `db` param; record news events + decisions; pass `decision_id` |
| Modify | `trading/position_monitor.py` | Pass `exit_reason` to `executor.sell()` |
| Modify | `main.py` | Create `TradeDB`, wire into `OrderExecutor` and `NewsHandler` |

---

## Task 1: Scaffolding — deps, .gitignore, data dir, config field

**Files:**
- Modify: `requirements.txt`
- Modify: `.gitignore`
- Create: `data/.gitkeep`
- Modify: `config.py`

- [ ] **Step 1: Add dependencies to requirements.txt**

Append three lines to `requirements.txt`:

```
fastapi>=0.115.0
uvicorn[standard]>=0.30.0
plotly>=5.22.0
```

- [ ] **Step 2: Add data/trades.db to .gitignore**

Add this line to `.gitignore`:

```
data/trades.db
```

- [ ] **Step 3: Create data/.gitkeep**

Create an empty file at `data/.gitkeep` so the directory is tracked by git.

- [ ] **Step 4: Add analytics_db_path to Config**

In `config.py`, add `analytics_db_path: str` to the `Config` dataclass (after `telegram_chat_id`):

```python
@dataclass(frozen=True)
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    tradier_access_token: str
    tradier_account_id: str
    tradier_paper: bool
    anthropic_api_key: str
    anthropic_model: str
    google_api_key: str
    gemini_model: str
    llm_provider: str
    trade_amount_usd: float
    short_qty: int
    allow_short: bool
    stop_loss_pct: float
    take_profit_pct: float
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
    analytics_db_path: str
```

In `load_config()`, add this line inside the `cfg = Config(...)` call (after `telegram_chat_id=...`):

```python
        analytics_db_path=os.getenv("ANALYTICS_DB_PATH", "data/trades.db"),
```

- [ ] **Step 5: Verify config still imports cleanly**

```bash
python -c "from config import load_config; print('OK')"
```

Expected: `OK` (will fail if required env vars are missing — that's fine, just check for ImportError)

- [ ] **Step 6: Commit**

```bash
git add requirements.txt .gitignore data/.gitkeep config.py
git commit -m "feat: scaffolding for trading analytics (deps, config, data dir)"
```

---

## Task 2: TradeDB — tests first, then implementation

**Files:**
- Create: `analytics/__init__.py`
- Create: `analytics/db.py`
- Create: `tests/test_analytics_db.py`

- [ ] **Step 1: Create package marker**

Create `analytics/__init__.py` as an empty file.

- [ ] **Step 2: Write the failing tests**

Create `tests/test_analytics_db.py`:

```python
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
    tid = db.record_trade_open(did, "AAPL", "buy", 3, 150.0, "2026-04-15T10:00:02Z")
    row = db._conn.execute(
        "SELECT decision_id, ticker, side, qty, entry_price, closed_at FROM trades WHERE id=?", (tid,)
    ).fetchone()
    assert row[0] == did
    assert row[1] == "AAPL"
    assert row[2] == "buy"
    assert row[3] == 3
    assert abs(row[4] - 150.0) < 0.001
    assert row[5] is None


def test_record_trade_open_without_decision(db):
    tid = db.record_trade_open(None, "TSLA", "short", 1, None, "2026-04-15T10:00:00Z")
    row = db._conn.execute("SELECT decision_id, entry_price FROM trades WHERE id=?", (tid,)).fetchone()
    assert row[0] is None
    assert row[1] is None


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
```

- [ ] **Step 3: Run tests to confirm they fail**

```bash
python -m pytest tests/test_analytics_db.py -v
```

Expected: all tests FAIL with `ModuleNotFoundError: No module named 'analytics'`

- [ ] **Step 4: Implement analytics/db.py**

Create `analytics/db.py`:

```python
# analytics/db.py
import sqlite3


class TradeDB:
    def __init__(self, path: str) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False)
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
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                decision_id  INTEGER REFERENCES llm_decisions(id),
                ticker       TEXT NOT NULL,
                side         TEXT NOT NULL,
                qty          INTEGER NOT NULL,
                entry_price  REAL,
                exit_price   REAL,
                pnl_usd      REAL,
                pnl_pct      REAL,
                exit_reason  TEXT,
                opened_at    TEXT NOT NULL,
                closed_at    TEXT
            );
        """)
        self._conn.commit()

    def record_news(self, ts: str, headline: str, summary: str | None, symbols: list[str]) -> int:
        cur = self._conn.execute(
            "INSERT INTO news_events (ts, headline, summary, symbols) VALUES (?, ?, ?, ?)",
            (ts, headline, summary, ",".join(symbols) if symbols else ""),
        )
        self._conn.commit()
        return cur.lastrowid  # type: ignore[return-value]

    def record_decision(
        self, news_event_id: int, ts: str, action: str, ticker: str | None, reasoning: str
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
    ) -> int:
        cur = self._conn.execute(
            "INSERT INTO trades (decision_id, ticker, side, qty, entry_price, opened_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (decision_id, ticker, side, qty, entry_price, opened_at),
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
        self._conn.execute(
            "UPDATE trades SET exit_price=?, pnl_usd=?, pnl_pct=?, exit_reason=?, closed_at=? WHERE id=?",
            (exit_price, pnl_usd, pnl_pct, exit_reason, closed_at, trade_id),
        )
        self._conn.commit()
```

- [ ] **Step 5: Run tests to confirm they pass**

```bash
python -m pytest tests/test_analytics_db.py -v
```

Expected: all 8 tests PASS

- [ ] **Step 6: Commit**

```bash
git add analytics/__init__.py analytics/db.py tests/test_analytics_db.py
git commit -m "feat: add TradeDB with SQLite schema and write methods"
```

---

## Task 3: Update OrderExecutor — DB wiring and position_book tuple change

**Files:**
- Modify: `trading/order_executor.py`
- Modify: `tests/test_order_executor.py`

> The `_position_book` value type changes from `tuple[float, int]` to `tuple[float, int, int | None]` — adding `trade_id` as the third element. Existing tests set this tuple directly and must be updated.

- [ ] **Step 1: Update existing tests that set _position_book directly**

In `tests/test_order_executor.py`, update the three tests that set `_position_book` to use 3-element tuples:

```python
# test_sell_increments_daily_sells — change:
ex._position_book["AAPL"] = (150.0, 1, None)

# test_sell_accumulates_realized_pnl — change:
ex._position_book["AAPL"] = (150.0, 2, None)

# test_sell_skips_pnl_when_already_provided — change:
ex._position_book["AAPL"] = (150.0, 1, None)
```

- [ ] **Step 2: Run existing tests to confirm they fail (tuple unpacking mismatch)**

```bash
python -m pytest tests/test_order_executor.py -v
```

Expected: 3 tests FAIL with `ValueError: not enough values to unpack` (the rest pass)

- [ ] **Step 3: Rewrite order_executor.py with DB wiring**

Replace `trading/order_executor.py` in full:

```python
# trading/order_executor.py
import asyncio
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import TYPE_CHECKING
import httpx
from trading.tradier_client import TradierClient
from config import Config
from notifications.telegram_notifier import Notifier

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


def _monday_of(d: date) -> date:
    return d - timedelta(days=d.weekday())


class OrderExecutor:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        held_tickers: set[str],
        shorted_tickers: set[str],
        notifier: Notifier,
        db: "TradeDB | None" = None,
    ) -> None:
        self._client = client
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._db = db
        self._pending_close: set[str] = set()
        # ticker -> (avg_entry_price, qty, trade_id); trade_id is None when db is disabled
        self._position_book: dict[str, tuple[float, int, int | None]] = {}
        # daily P&L counters — reset lazily at start of each new calendar day
        self._last_day: date = date.today()
        self._daily_buys: int = 0
        self._daily_sells: int = 0
        self._daily_realized_pnl: float = 0.0
        # weekly P&L counters — reset lazily at start of each new ISO week (Monday)
        self._last_week_monday: date = _monday_of(date.today())
        self._weekly_buys: int = 0
        self._weekly_sells: int = 0
        self._weekly_realized_pnl: float = 0.0

    @property
    def held_tickers(self) -> frozenset[str]:
        return frozenset(self._held_tickers)

    @property
    def shorted_tickers(self) -> frozenset[str]:
        return frozenset(self._shorted_tickers)

    @property
    def pending_close(self) -> frozenset[str]:
        return frozenset(self._pending_close)

    def confirm_closed(self, ticker: str) -> None:
        """Remove the pending-close guard once Tradier no longer returns the position."""
        self._pending_close.discard(ticker)

    def daily_summary(self) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for today. Resets counters on day boundary."""
        self._maybe_reset_day()
        return self._daily_buys, self._daily_sells, self._daily_realized_pnl

    def weekly_summary(self) -> tuple[int, int, float]:
        """Return (buys, sells, realized_pnl) for the current ISO week."""
        self._maybe_reset_week()
        return self._weekly_buys, self._weekly_sells, self._weekly_realized_pnl

    def _maybe_reset_day(self) -> None:
        today = date.today()
        if today != self._last_day:
            self._daily_buys = 0
            self._daily_sells = 0
            self._daily_realized_pnl = 0.0
            self._last_day = today

    def _maybe_reset_week(self) -> None:
        monday = _monday_of(date.today())
        if monday != self._last_week_monday:
            self._weekly_buys = 0
            self._weekly_sells = 0
            self._weekly_realized_pnl = 0.0
            self._last_week_monday = monday

    async def buy(self, ticker: str, decision_id: int | None = None) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            return
        try:
            quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
            price = quotes.get(ticker)
            if not price:
                logger.error("No quote available for %s — skipping buy", ticker)
                return
            qty = max(1, math.floor(self._notional_usd / price))
            order_id = await asyncio.to_thread(self._client.submit_order, ticker, "buy", qty)

            trade_id: int | None = None
            if self._db is not None:
                opened_at = datetime.now(timezone.utc).isoformat()
                trade_id = await asyncio.to_thread(
                    self._db.record_trade_open, decision_id, ticker, "buy", qty, price, opened_at
                )

            self._held_tickers.add(ticker)
            self._position_book[ticker] = (price, qty, trade_id)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1
            logger.info(
                "BUY order accepted for %s qty=%d @ $%.2f — order %s (pending fill)",
                ticker, qty, price, order_id,
            )
            await self._notifier.notify_buy(ticker, self._notional_usd, order_id)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._notifier.notify_error(f"buy {ticker}", str(e))

    async def short(self, ticker: str, decision_id: int | None = None) -> None:
        if ticker in self._shorted_tickers:
            logger.info("Skipping short for %s — already shorted", ticker)
            return
        if ticker in self._held_tickers:
            logger.info("Skipping short for %s — currently held long, sell first", ticker)
            return
        try:
            order_id = await asyncio.to_thread(
                self._client.submit_order, ticker, "sell_short", self._short_qty
            )

            trade_id = None
            if self._db is not None:
                opened_at = datetime.now(timezone.utc).isoformat()
                trade_id = await asyncio.to_thread(
                    self._db.record_trade_open,
                    decision_id, ticker, "short", self._short_qty, None, opened_at,
                )

            self._shorted_tickers.add(ticker)
            self._position_book[ticker] = (0.0, self._short_qty, trade_id)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_buys += 1
            self._weekly_buys += 1
            logger.info(
                "SHORT order accepted for %s qty=%d — order %s (pending fill)",
                ticker, self._short_qty, order_id,
            )
            await self._notifier.notify_short(ticker, self._short_qty, order_id)
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
            await self._notifier.notify_error(f"short {ticker}", str(e))

    async def sell(
        self,
        ticker: str,
        pnl_pct: float | None = None,
        pnl_usd: float | None = None,
        exit_reason: str = "llm",
    ) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return

        # Extract trade_id before modifying _position_book
        trade_id: int | None = None
        if ticker in self._position_book:
            _, _, trade_id = self._position_book[ticker]

        exit_price: float | None = None
        try:
            # Compute realized P&L for long positions when not already provided by caller
            if pnl_usd is None and ticker in self._held_tickers and ticker in self._position_book:
                entry_price, qty, _ = self._position_book[ticker]
                if entry_price > 0:
                    quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
                    current = quotes.get(ticker, 0.0)
                    if current:
                        exit_price = current
                        pnl_usd = (current - entry_price) * qty
                        pnl_pct = (current - entry_price) / entry_price

            await asyncio.to_thread(self._client.close_position, ticker)
            self._held_tickers.discard(ticker)
            self._shorted_tickers.discard(ticker)
            self._position_book.pop(ticker, None)
            self._pending_close.add(ticker)
            self._maybe_reset_day()
            self._maybe_reset_week()
            self._daily_sells += 1
            self._weekly_sells += 1
            if pnl_usd is not None:
                self._daily_realized_pnl += pnl_usd
                self._weekly_realized_pnl += pnl_usd

            if self._db is not None and trade_id is not None:
                closed_at = datetime.now(timezone.utc).isoformat()
                await asyncio.to_thread(
                    self._db.record_trade_close,
                    trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                )

            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
        except httpx.HTTPStatusError as e:
            if e.response.status_code in (400, 404):
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._pending_close.add(ticker)
                self._maybe_reset_day()
                self._maybe_reset_week()
                self._daily_sells += 1
                self._weekly_sells += 1
                if pnl_usd is not None:
                    self._daily_realized_pnl += pnl_usd
                    self._weekly_realized_pnl += pnl_usd
                if self._db is not None and trade_id is not None:
                    closed_at = datetime.now(timezone.utc).isoformat()
                    await asyncio.to_thread(
                        self._db.record_trade_close,
                        trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                    )
                logger.warning(
                    "Close %s — position already gone or closing (HTTP %s), removing from tracking",
                    ticker, e.response.status_code,
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except ValueError as e:
            if "no open position" in str(e).lower():
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._pending_close.add(ticker)
                self._maybe_reset_day()
                self._maybe_reset_week()
                self._daily_sells += 1
                self._weekly_sells += 1
                if pnl_usd is not None:
                    self._daily_realized_pnl += pnl_usd
                    self._weekly_realized_pnl += pnl_usd
                if self._db is not None and trade_id is not None:
                    closed_at = datetime.now(timezone.utc).isoformat()
                    await asyncio.to_thread(
                        self._db.record_trade_close,
                        trade_id, exit_price, pnl_usd, pnl_pct, exit_reason, closed_at,
                    )
                logger.warning(
                    "Close %s — position not found in broker, removing from tracking", ticker
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
```

- [ ] **Step 4: Run all executor tests**

```bash
python -m pytest tests/test_order_executor.py -v
```

Expected: all 14 tests PASS

- [ ] **Step 5: Run all tests to check for regressions**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add trading/order_executor.py tests/test_order_executor.py
git commit -m "feat: wire TradeDB into OrderExecutor; add exit_reason to sell()"
```

---

## Task 4: Update NewsHandler — record news events and LLM decisions

**Files:**
- Modify: `news/news_handler.py`

- [ ] **Step 1: Rewrite news_handler.py with DB wiring**

Replace `news/news_handler.py` in full:

```python
# news/news_handler.py
import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING
from alpaca.data.live import NewsDataStream
from trading.tradier_client import TradierClient
from llm.llm_advisor import LLMAdvisor
from trading.order_executor import OrderExecutor
from config import Config

if TYPE_CHECKING:
    from analytics.db import TradeDB

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        llm_advisor: LLMAdvisor,
        order_executor: OrderExecutor,
        db: "TradeDB | None" = None,
    ) -> None:
        self._client = client
        self._config = config
        self._advisor = llm_advisor
        self._executor = order_executor
        self._db = db

    async def run(self) -> None:
        while True:
            try:
                stream = NewsDataStream(
                    api_key=self._config.alpaca_api_key,
                    secret_key=self._config.alpaca_secret_key,
                )
                stream.subscribe_news(self._handle_news, "*")
                logger.info("News WebSocket connected — listening for news")
                # alpaca-py's public stream.run() calls asyncio.run() internally,
                # which conflicts with our event loop. We call _run_forever() directly
                # so the stream runs inside the same asyncio.gather loop as the
                # position monitor. Revisit if alpaca-py adds an async-native entry point.
                await stream._run_forever()
            except Exception:
                logger.exception("News stream error — reconnecting in 5s")
                await asyncio.sleep(5)

    async def _handle_news(self, news) -> None:
        try:
            clock = await asyncio.to_thread(self._client.get_clock)
            if not clock.is_open:
                logger.debug("Market closed — skipping news event")
                return
            headline = getattr(news, "headline", "")
            summary = getattr(news, "summary", "")
            symbols: list[str] = getattr(news, "symbols", [])

            logger.info("News received: %s | tickers: %s", headline, symbols)

            if not symbols:
                logger.debug("No tickers in news event — skipping")
                return

            news_event_id: int | None = None
            if self._db is not None:
                ts = datetime.now(timezone.utc).isoformat()
                news_event_id = await asyncio.to_thread(
                    self._db.record_news, ts, headline, summary, symbols
                )

            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
            )

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            decision_id: int | None = None
            if self._db is not None and news_event_id is not None:
                ts = datetime.now(timezone.utc).isoformat()
                decision_id = await asyncio.to_thread(
                    self._db.record_decision,
                    news_event_id, ts, decision.action, decision.ticker, decision.reasoning,
                )

            if decision.action == "buy" and decision.ticker:
                await self._executor.buy(decision.ticker, decision_id=decision_id)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(decision.ticker, decision_id=decision_id)
                else:
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")
```

- [ ] **Step 2: Verify import is clean**

```bash
python -c "from news.news_handler import NewsHandler; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add news/news_handler.py
git commit -m "feat: wire TradeDB into NewsHandler; record news events and LLM decisions"
```

---

## Task 5: Update PositionMonitor — pass exit_reason to sell()

**Files:**
- Modify: `trading/position_monitor.py`

- [ ] **Step 1: Update the two sell() calls in _check_positions to pass exit_reason**

In `trading/position_monitor.py`, find the two `await self._executor.sell(...)` calls inside `_check_positions` and add `exit_reason`:

```python
                if pnl <= -self._stop_loss:
                    logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd, exit_reason="stop_loss")
                elif pnl >= self._take_profit:
                    logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd, exit_reason="take_profit")
```

- [ ] **Step 2: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 3: Commit**

```bash
git add trading/position_monitor.py
git commit -m "feat: pass exit_reason (stop_loss/take_profit) to OrderExecutor.sell()"
```

---

## Task 6: Wire main.py — create TradeDB and inject into components

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Update main.py to create TradeDB and pass it to components**

Replace `main.py` in full:

```python
# main.py
import asyncio
import logging
import os
from rich.logging import RichHandler
from trading.tradier_client import TradierClient
from config import load_config, Config
from trading.order_executor import OrderExecutor
from llm.llm_advisor import LLMAdvisor
from news.news_handler import NewsHandler
from trading.position_monitor import PositionMonitor
from notifications.telegram_notifier import TelegramNotifier, TelegramCommandListener, NoOpNotifier

logging.basicConfig(
    level=logging.INFO,
    format="%(name)s — %(message)s",
    datefmt="[%X]",
    handlers=[RichHandler(rich_tracebacks=True, show_path=False)],
)
logger = logging.getLogger(__name__)


def _make_tradier_client(config: Config) -> TradierClient:
    return TradierClient(
        access_token=config.tradier_access_token,
        account_id=config.tradier_account_id,
        paper=config.tradier_paper,
    )


def _load_open_positions(client: TradierClient) -> tuple[set[str], set[str]]:
    positions = client.get_all_positions()
    held = {p.symbol for p in positions if p.qty > 0}
    shorted = {p.symbol for p in positions if p.qty < 0}
    if held:
        logger.info("Resuming with existing long positions: %s", held)
    if shorted:
        logger.info("Resuming with existing short positions: %s", shorted)
    return held, shorted


async def main() -> None:
    config = load_config()
    client = _make_tradier_client(config)
    try:
        held_tickers, shorted_tickers = _load_open_positions(client)

        if config.telegram_enabled:
            notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
        else:
            notifier = NoOpNotifier()

        db = None
        if config.analytics_db_path:
            from analytics.db import TradeDB
            os.makedirs(os.path.dirname(config.analytics_db_path) or ".", exist_ok=True)
            db = TradeDB(config.analytics_db_path)
            logger.info("Analytics DB: %s", config.analytics_db_path)

        order_executor = OrderExecutor(client, config, held_tickers, shorted_tickers, notifier, db)
        llm_advisor = LLMAdvisor(config)
        news_handler = NewsHandler(client, config, llm_advisor, order_executor, db)
        position_monitor = PositionMonitor(client, config, order_executor, notifier)

        coroutines = [news_handler.run(), position_monitor.run()]
        command_listener = None
        if config.telegram_enabled:
            command_listener = TelegramCommandListener(
                config.telegram_bot_token, config.telegram_chat_id, order_executor
            )
            coroutines.append(command_listener.run())

        logger.info(
            "Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
            config.tradier_paper, config.trade_amount_usd,
            config.stop_loss_pct * 100, config.take_profit_pct * 100,
        )

        try:
            await asyncio.gather(*coroutines)
        except asyncio.CancelledError:
            logger.info("Bot shutting down")
        finally:
            await notifier.aclose()
            if command_listener:
                await command_listener.aclose()
    finally:
        client.close()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify main imports cleanly**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: create TradeDB in main.py and inject into OrderExecutor and NewsHandler"
```

---

## Task 7: Analytics server — FastAPI + 6 Plotly charts

**Files:**
- Create: `analytics/server.py`

- [ ] **Step 1: Install new dependencies**

```bash
pip install fastapi "uvicorn[standard]" plotly
```

Expected: packages install without error

- [ ] **Step 2: Create analytics/server.py**

Create `analytics/server.py`:

```python
# analytics/server.py
import json
import os
import sqlite3

import plotly.graph_objects as go
import plotly.utils
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DB_PATH = os.getenv("ANALYTICS_DB_PATH", "data/trades.db")

app = FastAPI()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fig_json(fig: go.Figure) -> dict:
    return json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig))


def _build_charts() -> tuple[dict, list[dict]]:
    con = _conn()

    # 1 & 2: Cumulative and daily P&L
    rows = con.execute(
        "SELECT date(closed_at) as day, SUM(pnl_usd) as dpnl "
        "FROM trades WHERE pnl_usd IS NOT NULL AND closed_at IS NOT NULL "
        "GROUP BY day ORDER BY day"
    ).fetchall()
    days = [r["day"] for r in rows]
    daily_pnl = [r["dpnl"] for r in rows]
    cumulative: list[float] = []
    total = 0.0
    for p in daily_pnl:
        total += p
        cumulative.append(total)

    fig_cum = go.Figure(go.Scatter(x=days, y=cumulative, mode="lines+markers"))
    fig_cum.update_layout(title="Cumulative P&L", xaxis_title="Date", yaxis_title="USD")

    fig_daily = go.Figure(go.Bar(x=days, y=daily_pnl))
    fig_daily.update_layout(title="Daily P&L", xaxis_title="Date", yaxis_title="USD")

    # 3: Exit reason donut
    exit_rows = con.execute(
        "SELECT exit_reason, COUNT(*) as cnt FROM trades "
        "WHERE exit_reason IS NOT NULL GROUP BY exit_reason"
    ).fetchall()
    fig_exit = go.Figure(go.Pie(
        labels=[r["exit_reason"] for r in exit_rows],
        values=[r["cnt"] for r in exit_rows],
        hole=0.4,
    ))
    fig_exit.update_layout(title="Exit Reason Distribution")

    # 4: P&L % distribution
    pct_rows = con.execute("SELECT pnl_pct FROM trades WHERE pnl_pct IS NOT NULL").fetchall()
    pcts = [r["pnl_pct"] * 100 for r in pct_rows]
    fig_dist = go.Figure(go.Histogram(x=pcts, nbinsx=20))
    fig_dist.update_layout(title="P&L % Distribution at Exit", xaxis_title="P&L %", yaxis_title="Count")

    # 5: Trade duration histogram
    dur_rows = con.execute(
        "SELECT (julianday(closed_at) - julianday(opened_at)) * 24 * 60 AS mins "
        "FROM trades WHERE closed_at IS NOT NULL AND opened_at IS NOT NULL"
    ).fetchall()
    durations = [r["mins"] for r in dur_rows if r["mins"] is not None]
    fig_dur = go.Figure(go.Histogram(x=durations, nbinsx=20))
    fig_dur.update_layout(title="Trade Duration", xaxis_title="Minutes held", yaxis_title="Count")

    # 6: LLM decision counts
    action_rows = con.execute(
        "SELECT action, COUNT(*) as cnt FROM llm_decisions GROUP BY action"
    ).fetchall()
    fig_actions = go.Figure(go.Bar(
        x=[r["action"] for r in action_rows],
        y=[r["cnt"] for r in action_rows],
    ))
    fig_actions.update_layout(title="LLM Decision Counts", xaxis_title="Action", yaxis_title="Count")

    # Recent news → decision → outcome table
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason "
        "FROM news_events n "
        "JOIN llm_decisions d ON d.news_event_id = n.id "
        "LEFT JOIN trades t ON t.decision_id = d.id "
        "ORDER BY n.ts DESC LIMIT 20"
    ).fetchall()

    con.close()

    charts = {
        "cumulative": _fig_json(fig_cum),
        "daily": _fig_json(fig_daily),
        "exit": _fig_json(fig_exit),
        "dist": _fig_json(fig_dist),
        "duration": _fig_json(fig_dur),
        "actions": _fig_json(fig_actions),
    }
    return charts, [dict(r) for r in recent]


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    charts, recent = _build_charts()

    chart_divs = ""
    for key, fig_data in charts.items():
        chart_divs += (
            f'<div id="c-{key}" style="margin-bottom:40px"></div>\n'
            f'<script>Plotly.newPlot("c-{key}",'
            f'{json.dumps(fig_data["data"])},{json.dumps(fig_data["layout"])})</script>\n'
        )

    table_rows = ""
    for r in recent:
        pnl_usd = f"{r['pnl_usd']:+.2f}" if r["pnl_usd"] is not None else "—"
        pnl_pct = f"{r['pnl_pct'] * 100:+.1f}%" if r["pnl_pct"] is not None else "—"
        headline = (r["headline"] or "")[:60]
        table_rows += (
            f"<tr>"
            f"<td>{(r['ts'] or '')[:16]}</td>"
            f"<td>{headline}</td>"
            f"<td>{r['action']}</td>"
            f"<td>{r['ticker'] or '—'}</td>"
            f"<td>{pnl_usd}</td>"
            f"<td>{pnl_pct}</td>"
            f"<td>{r['exit_reason'] or '—'}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trading Analytics</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1 {{ border-bottom: 2px solid #eee; padding-bottom: 8px; }}
  h2 {{ margin-top: 48px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 7px 10px; text-align: left; }}
  th {{ background: #f6f6f6; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
</style>
</head>
<body>
<h1>Trading Analytics</h1>
{chart_divs}
<h2>Recent Trades (last 20)</h2>
<table>
<thead>
  <tr><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
      <th>P&amp;L USD</th><th>P&amp;L %</th><th>Exit</th></tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    uvicorn.run("analytics.server:app", host="0.0.0.0", port=8080, reload=False)
```

- [ ] **Step 3: Verify server imports cleanly**

```bash
python -c "from analytics.server import app; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Run all tests one final time**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add analytics/server.py
git commit -m "feat: add FastAPI analytics server with 6 Plotly charts"
```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Covered by |
|---|---|
| SQLite `news_events` table | Task 2 — `TradeDB._create_tables()` |
| SQLite `llm_decisions` table | Task 2 — `TradeDB._create_tables()` |
| SQLite `trades` table with all columns | Task 2 — `TradeDB._create_tables()` |
| `record_news`, `record_decision`, `record_trade_open`, `record_trade_close` | Task 2 — `TradeDB` methods |
| `OrderExecutor` gains optional `db` param | Task 3 |
| `_position_book` stores `trade_id` | Task 3 |
| `buy()` records trade open | Task 3 |
| `short()` records trade open with `entry_price=None` | Task 3 |
| `sell()` records trade close in all exit paths | Task 3 |
| `db=None` disables recording (existing tests unbroken) | Task 3 |
| `NewsHandler` records news + decision, passes `decision_id` | Task 4 |
| `PositionMonitor` passes `exit_reason` | Task 5 |
| `main.py` creates `TradeDB` from config path | Task 6 |
| `ANALYTICS_DB_PATH` env var | Task 1 (config) + Task 6 (main.py) |
| `data/.gitkeep`, `data/trades.db` gitignored | Task 1 |
| Cumulative P&L chart | Task 7 |
| Daily P&L bars | Task 7 |
| SL/TP hit rate | Task 7 |
| P&L % distribution histogram | Task 7 |
| Trade duration histogram | Task 7 |
| LLM decision counts + recent table | Task 7 |
| `python -m analytics.server` entry point | Task 7 (`if __name__ == "__main__"`) |
| Tests for `TradeDB` with `:memory:` | Task 2 |

No gaps found.
