# Tradier Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the Alpaca trading layer with Tradier's REST API while keeping Alpaca's `NewsDataStream` for real-time news, and remove the PDT same-day trading guard.

**Architecture:** A new `TradierClient` wraps `httpx` (sync, called via `asyncio.to_thread`). `OrderExecutor` and `PositionMonitor` swap their `TradingClient` dependency for `TradierClient`. EOD/weekly P&L reports are now computed from in-memory counters in `OrderExecutor` instead of Alpaca's portfolio history API. The `alpaca` branch preserves the original implementation untouched.

**Tech Stack:** Python 3.11+, `httpx` (already in requirements), `alpaca-py` (kept for `NewsDataStream` only), `pytest`, `unittest.mock`

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `trading/tradier_client.py` | **Create** | httpx wrapper + dataclasses + pure parsing functions |
| `tests/test_tradier_client.py` | **Create** | Tests for pure parsing functions in tradier_client |
| `config.py` | **Modify** | Remove `alpaca_base_url`; add `tradier_access_token`, `tradier_account_id`, `tradier_paper` |
| `trading/order_executor.py` | **Rewrite** | Use `TradierClient`; remove PDT guard; add in-memory P&L tracking |
| `tests/test_order_executor.py` | **Rewrite** | Replace all PDT tests with P&L tracking tests |
| `trading/position_monitor.py` | **Rewrite** | Use `TradierClient`; remove PDT checks; read P&L from executor |
| `news/news_handler.py` | **Modify** | Swap `TradingClient` → `TradierClient` for clock check only |
| `main.py` | **Rewrite** | New Tradier factory; simplified startup without PDT seeding |
| `.env.example` | **Modify** | Replace `ALPACA_BASE_URL` with Tradier vars |

---

## Task 1: Create the `alpaca` branch

**Files:**
- No file changes — git operations only

- [ ] **Step 1: Create and push the alpaca branch**

```bash
git checkout -b alpaca
git push -u origin alpaca
git checkout master
```

Expected: branch `alpaca` created pointing at current master HEAD; you are back on `master`.

- [ ] **Step 2: Verify**

```bash
git branch -a
```

Expected output includes `alpaca` and `* master`.

---

## Task 2: Create `trading/tradier_client.py`

**Files:**
- Create: `trading/tradier_client.py`

- [ ] **Step 1: Write the file**

```python
# trading/tradier_client.py
import httpx
from dataclasses import dataclass


@dataclass
class TradierClock:
    is_open: bool


@dataclass
class TradierPosition:
    symbol: str
    qty: float       # positive = long, negative = short
    cost_basis: float  # total cost basis (dollars), not per-share


class TradierClient:
    _LIVE_BASE = "https://api.tradier.com/v1"
    _SANDBOX_BASE = "https://sandbox.tradier.com/v1"

    def __init__(self, access_token: str, account_id: str, paper: bool = True) -> None:
        self._account_id = account_id
        base = self._SANDBOX_BASE if paper else self._LIVE_BASE
        self._http = httpx.Client(
            base_url=base,
            headers={
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/json",
            },
            timeout=10.0,
        )

    def get_clock(self) -> TradierClock:
        resp = self._http.get("/markets/clock")
        resp.raise_for_status()
        state = resp.json()["clock"]["state"]
        return TradierClock(is_open=(state == "open"))

    def get_all_positions(self) -> list[TradierPosition]:
        resp = self._http.get(f"/accounts/{self._account_id}/positions")
        resp.raise_for_status()
        return _parse_positions(resp.json())

    def get_quotes(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        resp = self._http.get(
            "/markets/quotes",
            params={"symbols": ",".join(symbols)},
        )
        resp.raise_for_status()
        return _parse_quotes(resp.json())

    def submit_order(self, symbol: str, side: str, qty: int) -> str:
        """Side: buy | sell | sell_short | buy_to_cover"""
        resp = self._http.post(
            f"/accounts/{self._account_id}/orders",
            data={
                "class": "equity",
                "symbol": symbol,
                "side": side,
                "quantity": str(qty),
                "type": "market",
                "duration": "day",
            },
        )
        resp.raise_for_status()
        return str(resp.json()["order"]["id"])

    def close_position(self, symbol: str) -> str:
        """Sell long or cover short — looks up current position to determine side/qty."""
        positions = self.get_all_positions()
        pos = next((p for p in positions if p.symbol == symbol), None)
        if pos is None:
            raise ValueError(f"No open position for {symbol}")
        side = "sell" if pos.qty > 0 else "buy_to_cover"
        qty = max(1, abs(int(pos.qty)))
        return self.submit_order(symbol, side, qty)

    def close(self) -> None:
        self._http.close()


def _parse_positions(data: dict) -> list[TradierPosition]:
    """Parse Tradier positions response. Handles null, single object, and array."""
    raw = data.get("positions")
    if raw is None or raw == "null":
        return []
    pos_data = raw.get("position")
    if pos_data is None:
        return []
    if isinstance(pos_data, dict):
        pos_data = [pos_data]
    return [
        TradierPosition(
            symbol=p["symbol"],
            qty=float(p["quantity"]),
            cost_basis=float(p["cost_basis"]),
        )
        for p in pos_data
    ]


def _parse_quotes(data: dict) -> dict[str, float]:
    """Parse Tradier quotes response. Handles single quote and array."""
    raw = data.get("quotes", {})
    quote_data = raw.get("quote")
    if quote_data is None:
        return {}
    if isinstance(quote_data, dict):
        quote_data = [quote_data]
    return {q["symbol"]: float(q["last"]) for q in quote_data}
```

- [ ] **Step 2: Verify the module imports cleanly**

```bash
python -c "from trading.tradier_client import TradierClient, TradierClock, TradierPosition, _parse_positions, _parse_quotes; print('OK')"
```

Expected: `OK`

---

## Task 3: Write `tests/test_tradier_client.py`

**Files:**
- Create: `tests/test_tradier_client.py`

- [ ] **Step 1: Write the tests**

```python
# tests/test_tradier_client.py
from trading.tradier_client import _parse_positions, _parse_quotes, TradierPosition


def test_parse_positions_null_string():
    assert _parse_positions({"positions": "null"}) == []


def test_parse_positions_none_value():
    assert _parse_positions({"positions": None}) == []


def test_parse_positions_missing_key():
    assert _parse_positions({}) == []


def test_parse_positions_single_object():
    data = {
        "positions": {
            "position": {"symbol": "AAPL", "quantity": 2.0, "cost_basis": 300.0}
        }
    }
    result = _parse_positions(data)
    assert result == [TradierPosition(symbol="AAPL", qty=2.0, cost_basis=300.0)]


def test_parse_positions_multiple():
    data = {
        "positions": {
            "position": [
                {"symbol": "AAPL", "quantity": 2.0, "cost_basis": 300.0},
                {"symbol": "MSFT", "quantity": -1.0, "cost_basis": 400.0},
            ]
        }
    }
    result = _parse_positions(data)
    assert len(result) == 2
    assert result[0].symbol == "AAPL"
    assert result[1].qty == -1.0


def test_parse_positions_long_positive_short_negative():
    data = {
        "positions": {
            "position": {"symbol": "TSLA", "quantity": -3.0, "cost_basis": 900.0}
        }
    }
    result = _parse_positions(data)
    assert result[0].qty < 0


def test_parse_quotes_single():
    data = {"quotes": {"quote": {"symbol": "AAPL", "last": 175.5}}}
    assert _parse_quotes(data) == {"AAPL": 175.5}


def test_parse_quotes_multiple():
    data = {
        "quotes": {
            "quote": [
                {"symbol": "AAPL", "last": 175.5},
                {"symbol": "MSFT", "last": 420.0},
            ]
        }
    }
    assert _parse_quotes(data) == {"AAPL": 175.5, "MSFT": 420.0}


def test_parse_quotes_empty_quotes():
    assert _parse_quotes({"quotes": {}}) == {}


def test_parse_quotes_missing_key():
    assert _parse_quotes({}) == {}
```

- [ ] **Step 2: Run the tests**

```bash
python -m pytest tests/test_tradier_client.py -v
```

Expected: 9 tests pass, 0 fail.

- [ ] **Step 3: Commit**

```bash
git add trading/tradier_client.py tests/test_tradier_client.py
git commit -m "feat: add TradierClient with pure parsing functions and tests"
```

---

## Task 4: Update `config.py`

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Replace `config.py`**

```python
# config.py
import os
from dataclasses import dataclass
from dotenv import load_dotenv


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


def _parse_float(key: str, default: str) -> float:
    raw = os.getenv(key, default)
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"Environment variable {key}={raw!r} is not a valid float")


def load_config() -> Config:
    load_dotenv()

    provider = os.getenv("LLM_PROVIDER", "claude").lower()
    if provider not in ("claude", "gemini"):
        raise ValueError(f"LLM_PROVIDER must be 'claude' or 'gemini', got {provider!r}")

    required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID"]
    if provider == "claude":
        required.append("ANTHROPIC_API_KEY")
    else:
        required.append("GOOGLE_API_KEY")

    missing = [k for k in required if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
    if telegram_enabled:
        telegram_missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(k)]
        if telegram_missing:
            raise ValueError(f"TELEGRAM_ENABLED=true but missing: {', '.join(telegram_missing)}")

    cfg = Config(
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        tradier_access_token=os.environ["TRADIER_ACCESS_TOKEN"],
        tradier_account_id=os.environ["TRADIER_ACCOUNT_ID"],
        tradier_paper=os.getenv("TRADIER_PAPER", "true").lower() in ("true", "1", "yes"),
        anthropic_api_key=os.getenv("ANTHROPIC_API_KEY", ""),
        anthropic_model=os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6"),
        google_api_key=os.getenv("GOOGLE_API_KEY", ""),
        gemini_model=os.getenv("GEMINI_MODEL", "gemini-2.0-flash"),
        llm_provider=provider,
        trade_amount_usd=_parse_float("TRADE_AMOUNT_USD", "5.0"),
        short_qty=int(os.getenv("SHORT_QTY", "1")),
        allow_short=os.getenv("ALLOW_SHORT", "true").lower() in ("true", "1", "yes"),
        stop_loss_pct=_parse_float("STOP_LOSS_PCT", "2.0") / 100,
        take_profit_pct=_parse_float("TAKE_PROFIT_PCT", "3.0") / 100,
        telegram_enabled=telegram_enabled,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
    )

    if cfg.trade_amount_usd <= 0:
        raise ValueError("TRADE_AMOUNT_USD must be positive")
    if cfg.short_qty <= 0:
        raise ValueError("SHORT_QTY must be a positive integer")
    if not (0 < cfg.stop_loss_pct < 1):
        raise ValueError("STOP_LOSS_PCT must be between 0 and 100 exclusive (e.g. 2 = 2%)")
    if not (0 < cfg.take_profit_pct < 1):
        raise ValueError("TAKE_PROFIT_PCT must be between 0 and 100 exclusive (e.g. 3 = 3%)")

    return cfg
```

- [ ] **Step 2: Verify import**

```bash
python -c "from config import load_config; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: replace alpaca_base_url with tradier config fields"
```

---

## Task 5: Rewrite `trading/order_executor.py`

**Files:**
- Modify: `trading/order_executor.py`

- [ ] **Step 1: Replace the file**

```python
# trading/order_executor.py
import asyncio
import logging
import math
from datetime import date, timedelta
from trading.tradier_client import TradierClient
from config import Config
from notifications.telegram_notifier import Notifier

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
    ) -> None:
        self._client = client
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._pending_close: set[str] = set()
        # ticker -> (avg_entry_price, qty); used to compute realized P&L
        self._position_book: dict[str, tuple[float, int]] = {}
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

    async def buy(self, ticker: str) -> None:
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
            self._held_tickers.add(ticker)
            self._position_book[ticker] = (price, qty)
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

    async def short(self, ticker: str) -> None:
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
            self._shorted_tickers.add(ticker)
            self._position_book[ticker] = (0.0, self._short_qty)  # entry unknown; P&L skipped on cover
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

    async def sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return
        try:
            # Compute realized P&L for long positions when not already provided by caller
            if pnl_usd is None and ticker in self._held_tickers and ticker in self._position_book:
                entry_price, qty = self._position_book[ticker]
                if entry_price > 0:
                    quotes = await asyncio.to_thread(self._client.get_quotes, [ticker])
                    current = quotes.get(ticker, 0.0)
                    if current:
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
            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker, pnl_pct, pnl_usd)
        except Exception as e:
            body = str(e).lower()
            if "404" in body or "400" in body or "no open position" in body:
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                self._position_book.pop(ticker, None)
                self._pending_close.add(ticker)
                logger.warning(
                    "Close %s — position already gone or closing, removing from tracking", ticker
                )
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
```

- [ ] **Step 2: Verify import**

```bash
python -c "from trading.order_executor import OrderExecutor, _monday_of; print('OK')"
```

Expected: `OK`

---

## Task 6: Replace `tests/test_order_executor.py`

**Files:**
- Modify: `tests/test_order_executor.py`

- [ ] **Step 1: Replace the file**

```python
# tests/test_order_executor.py
from datetime import date, timedelta
from unittest.mock import MagicMock, AsyncMock
import pytest
from trading.order_executor import OrderExecutor, _monday_of
from config import Config


def _make_executor() -> OrderExecutor:
    client = MagicMock()
    config = MagicMock(spec=Config)
    config.trade_amount_usd = 100.0
    config.short_qty = 1
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    return OrderExecutor(client, config, set(), set(), notifier)


# --- _monday_of ---

def test_monday_of_monday():
    d = date(2026, 4, 13)  # Monday
    assert _monday_of(d) == date(2026, 4, 13)


def test_monday_of_friday():
    d = date(2026, 4, 17)  # Friday
    assert _monday_of(d) == date(2026, 4, 13)


def test_monday_of_sunday():
    d = date(2026, 4, 19)  # Sunday
    assert _monday_of(d) == date(2026, 4, 13)


# --- daily_summary ---

def test_daily_summary_initial_state():
    ex = _make_executor()
    buys, sells, pnl = ex.daily_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_daily_summary_resets_on_new_day():
    ex = _make_executor()
    ex._daily_buys = 3
    ex._daily_sells = 2
    ex._daily_realized_pnl = 12.50
    ex._last_day = date.today() - timedelta(days=1)  # simulate stale day
    buys, sells, pnl = ex.daily_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_daily_summary_does_not_reset_same_day():
    ex = _make_executor()
    ex._daily_buys = 2
    ex._daily_sells = 1
    ex._daily_realized_pnl = 5.0
    buys, sells, pnl = ex.daily_summary()
    assert buys == 2
    assert sells == 1
    assert pnl == 5.0


# --- weekly_summary ---

def test_weekly_summary_initial_state():
    ex = _make_executor()
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_weekly_summary_resets_on_new_week():
    ex = _make_executor()
    ex._weekly_buys = 5
    ex._weekly_sells = 3
    ex._weekly_realized_pnl = 25.0
    ex._last_week_monday = _monday_of(date.today()) - timedelta(weeks=1)  # last week
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 0
    assert sells == 0
    assert pnl == 0.0


def test_weekly_summary_does_not_reset_same_week():
    ex = _make_executor()
    ex._weekly_buys = 4
    ex._weekly_sells = 2
    ex._weekly_realized_pnl = 18.0
    buys, sells, pnl = ex.weekly_summary()
    assert buys == 4
    assert sells == 2
    assert pnl == 18.0


# --- pending_close / confirm_closed ---

def test_confirm_closed_removes_ticker():
    ex = _make_executor()
    ex._pending_close.add("AAPL")
    ex.confirm_closed("AAPL")
    assert "AAPL" not in ex.pending_close


def test_confirm_closed_noop_for_unknown_ticker():
    ex = _make_executor()
    ex.confirm_closed("AAPL")  # should not raise
    assert "AAPL" not in ex.pending_close
```

- [ ] **Step 2: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass (including `test_tradier_client.py` and `test_position_monitor.py`).

- [ ] **Step 3: Commit**

```bash
git add trading/order_executor.py tests/test_order_executor.py
git commit -m "feat: rewrite OrderExecutor for Tradier with in-memory P&L tracking; remove PDT guard"
```

---

## Task 7: Rewrite `trading/position_monitor.py`

**Files:**
- Modify: `trading/position_monitor.py`

- [ ] **Step 1: Replace the file**

```python
# trading/position_monitor.py
import asyncio
import logging
from datetime import date, datetime
import pytz
from trading.tradier_client import TradierClient
from trading.order_executor import OrderExecutor
from notifications.telegram_notifier import Notifier
from config import Config

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


def _should_fire_report(now_et: datetime, last_report_date: date | None) -> bool:
    """Return True if the EOD/weekly report should fire now.

    Fires during the 16:00–16:01 ET window on weekdays, at most once per calendar day.
    """
    if now_et.tzinfo is None:
        raise ValueError("now_et must be timezone-aware (ET)")
    if now_et.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    if now_et.hour != 16 or now_et.minute > 1:
        return False
    return last_report_date != now_et.date()


class PositionMonitor:
    def __init__(
        self,
        client: TradierClient,
        config: Config,
        order_executor: OrderExecutor,
        notifier: Notifier,
    ) -> None:
        self._client = client
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor
        self._notifier = notifier
        self._last_report_date: date | None = None

    async def run(self) -> None:
        await asyncio.gather(self._position_loop(), self._report_loop())

    async def _position_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")

    async def _report_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._check_report()
            except Exception:
                logger.exception("Report loop error")

    async def _check_report(self) -> None:
        et = pytz.timezone("America/New_York")
        now = datetime.now(et)
        if not _should_fire_report(now, self._last_report_date):
            return

        today = now.date()
        buys, sells, pnl = self._fetch_eod_data()
        await self._notifier.notify_eod_report(buys, sells, pnl)
        self._last_report_date = today
        logger.info("EOD report sent: buys=%d sells=%d pnl=%.2f", buys, sells, pnl)

        if today.weekday() == 4:  # Friday
            w_buys, w_sells, w_pnl = self._fetch_weekly_data()
            await self._notifier.notify_weekly_report(w_buys, w_sells, w_pnl)
            logger.info("Weekly report sent: buys=%d sells=%d pnl=%.2f", w_buys, w_sells, w_pnl)

    def _fetch_eod_data(self) -> tuple[int, int, float]:
        return self._executor.daily_summary()

    def _fetch_weekly_data(self) -> tuple[int, int, float]:
        return self._executor.weekly_summary()

    async def _check_positions(self) -> None:
        positions = await asyncio.to_thread(self._client.get_all_positions)
        live_symbols = {pos.symbol for pos in positions}

        # Confirm tickers that Tradier no longer returns
        for ticker in self._executor.pending_close - live_symbols:
            self._executor.confirm_closed(ticker)
            logger.info("Confirmed closed: %s no longer in Tradier positions", ticker)

        open_positions = [
            pos for pos in positions if pos.symbol not in self._executor.pending_close
        ]
        if not open_positions:
            return

        symbols = [pos.symbol for pos in open_positions]
        quotes = await asyncio.to_thread(self._client.get_quotes, symbols)

        for pos in open_positions:
            try:
                ticker = pos.symbol
                qty = abs(pos.qty)
                if qty == 0:
                    continue
                # cost_basis is the total cost (e.g. $300 for 2 shares at $150 avg)
                entry = pos.cost_basis / qty
                if entry == 0.0:
                    logger.warning("Skipping %s — entry price is zero", ticker)
                    continue

                current = quotes.get(ticker)
                if current is None:
                    logger.warning("No quote for %s — skipping", ticker)
                    continue

                pnl = compute_pnl_pct(entry, current)
                pnl_usd = (current - entry) * qty

                if pnl <= -self._stop_loss:
                    logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
                elif pnl >= self._take_profit:
                    logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
```

- [ ] **Step 2: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Commit**

```bash
git add trading/position_monitor.py
git commit -m "feat: rewrite PositionMonitor for Tradier; read P&L from executor; remove PDT checks"
```

---

## Task 8: Update `news/news_handler.py`

**Files:**
- Modify: `news/news_handler.py`

- [ ] **Step 1: Replace the Alpaca client import and type hint**

Change line 4 from:
```python
from alpaca.trading.client import TradingClient
```
To:
```python
from trading.tradier_client import TradierClient
```

Change the constructor parameter type hint on line 13 from:
```python
def __init__(self, client: TradingClient, config: Config, llm_advisor: LLMAdvisor, order_executor: OrderExecutor) -> None:
```
To:
```python
def __init__(self, client: TradierClient, config: Config, llm_advisor: LLMAdvisor, order_executor: OrderExecutor) -> None:
```

The body of `_handle_news` already calls `self._client.get_clock()` — no other changes needed since `TradierClient.get_clock()` returns `TradierClock` which also has `is_open: bool`.

- [ ] **Step 2: Verify import**

```bash
python -c "from news.news_handler import NewsHandler; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 4: Commit**

```bash
git add news/news_handler.py
git commit -m "feat: update NewsHandler to use TradierClient for market clock"
```

---

## Task 9: Rewrite `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Replace the file**

```python
# main.py
import asyncio
import logging
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
    held_tickers, shorted_tickers = _load_open_positions(client)

    if config.telegram_enabled:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        notifier = NoOpNotifier()

    order_executor = OrderExecutor(client, config, held_tickers, shorted_tickers, notifier)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(client, config, llm_advisor, order_executor)
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
        client.close()
        await notifier.aclose()
        if command_listener:
            await command_listener.aclose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify import**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: rewrite main.py for Tradier; remove Alpaca trading client factory and PDT seeding"
```

---

## Task 10: Update `.env.example`

**Files:**
- Modify: `.env.example`

- [ ] **Step 1: Replace the file**

```
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key

TRADIER_ACCESS_TOKEN=your_tradier_access_token
TRADIER_ACCOUNT_ID=your_tradier_account_id
TRADIER_PAPER=true

# LLM provider: "claude" or "gemini"
LLM_PROVIDER=gemini

# Claude (required if LLM_PROVIDER=claude)
ANTHROPIC_API_KEY=your_anthropic_api_key
ANTHROPIC_MODEL=claude-opus-4-6

# Gemini (required if LLM_PROVIDER=gemini)
GOOGLE_API_KEY=your_google_api_key
GEMINI_MODEL=gemini-2.0-flash

TRADE_AMOUNT_USD=5.0
ALLOW_SHORT=false
SHORT_QTY=1
STOP_LOSS_PCT=2
TAKE_PROFIT_PCT=3

# Telegram notifications
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

- [ ] **Step 2: Run full test suite one final time**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 3: Verify main.py imports cleanly**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 4: Final commit**

```bash
git add .env.example
git commit -m "chore: update .env.example for Tradier migration"
```
