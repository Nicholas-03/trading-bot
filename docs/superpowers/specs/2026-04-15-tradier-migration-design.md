# Tradier Migration Design

**Date:** 2026-04-15
**Status:** Approved

## Overview

Migrate the trading layer from Alpaca to Tradier while preserving the existing Alpaca `NewsDataStream` for real-time news. The current implementation is preserved on an `alpaca` branch; master gets a clean Tradier-based implementation with the PDT day-trading guard removed.

## Branch Strategy

1. Create `alpaca` branch from current `master` — preserves all Alpaca code untouched.
2. All Tradier work happens on `master`.

## What Is Removed

- `alpaca_base_url` from `Config` and `.env.example`
- `paper` property on `Config` (replaced by `tradier_paper`)
- PDT guard entirely: `_open_dates`, `is_opened_today`, `open_dates` constructor param in `OrderExecutor`; guard checks in `PositionMonitor`
- `_load_open_positions()` in `main.py` (seeded PDT open_dates from Alpaca order history)
- Alpaca portfolio history queries: `_fetch_eod_data` / `_fetch_weekly_data` Alpaca API calls
- `alpaca-py` trading imports from `order_executor.py`, `position_monitor.py`, `main.py`

## What Is Kept

- `alpaca_api_key` + `alpaca_secret_key` in `Config` — still required for `NewsDataStream`
- `alpaca-py` in `requirements.txt` — still required for `NewsDataStream`
- All LLM provider logic (Claude/Gemini)
- All Telegram notification logic
- `buy` / `sell` / `short` interface on `OrderExecutor`
- SL/TP loop in `PositionMonitor`
- EOD/weekly report scheduling logic

## New Components

### `trading/tradier_client.py`

Synchronous `httpx` wrapper (consistent with how Alpaca's client was used — wrapped in `asyncio.to_thread` at call sites).

**Base URLs:**
- Paper/sandbox: `https://sandbox.tradier.com/v1`
- Live: `https://api.tradier.com/v1`

**Auth:** `Authorization: Bearer {token}` + `Accept: application/json` on every request.

**Data classes:**
```python
@dataclass
class TradierClock:
    is_open: bool

@dataclass
class TradierPosition:
    symbol: str
    qty: float          # positive = long, negative = short
    cost_basis: float   # used as avg entry price
```

**Methods:**

| Method | Endpoint | Notes |
|--------|----------|-------|
| `get_clock() -> TradierClock` | `GET /markets/clock` | `state == "open"` → is_open |
| `get_all_positions() -> list[TradierPosition]` | `GET /accounts/{id}/positions` | handles "null" positions response |
| `get_quotes(symbols: list[str]) -> dict[str, float]` | `GET /markets/quotes?symbols=A,B` | returns last price per symbol |
| `submit_order(symbol, side, qty) -> str` | `POST /accounts/{id}/orders` | `class=equity`, `type=market`, `duration=day`; returns order id |
| `close_position(symbol) -> str` | looks up position, then `submit_order` | qty>0 → `sell`; qty<0 → `buy_to_cover` |

## Updated Components

### `config.py`

**Removed fields:** `alpaca_base_url`

**Added fields:**
- `tradier_access_token: str`
- `tradier_account_id: str`
- `tradier_paper: bool`

**Required env vars:** `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, `TRADIER_ACCESS_TOKEN`, `TRADIER_ACCOUNT_ID`

### `trading/order_executor.py`

**Removed:** `open_dates` param, `_open_dates`, `is_opened_today`, `TradingClient` import, Alpaca `APIError` handling

**Changed dependency:** `TradingClient` → `TradierClient`

**`buy()` flow:**
1. Call `TradierClient.get_quotes([ticker])` to get current price
2. Compute `qty = max(1, floor(notional_usd / price))`
3. Submit `side=buy` order
4. Record `_position_book[ticker] = (price, qty)`

**`short()` flow:**
1. Submit `side=sell_short` with `config.short_qty`
2. Record `_position_book[ticker] = (0.0, short_qty)` — entry price unknown for shorts; P&L skipped on cover

**`sell()` flow:**
1. Fetch current quote for P&L computation
2. Compute `realized_pnl = (current_price - avg_entry) * qty` (long only; shorts skip P&L calc)
3. Submit via `TradierClient.close_position(ticker)`
4. Accumulate `_daily_realized_pnl`, `_daily_sells`
5. Error handling: catch HTTP 400/404 (position already gone) same pattern as current Alpaca 404/422 handler

**In-memory P&L tracking:**
- `_position_book: dict[str, tuple[float, int]]` — `ticker → (avg_entry, qty)`
- `_daily_buys: int`, `_daily_sells: int`, `_daily_realized_pnl: float`
- `_weekly_buys: int`, `_weekly_sells: int`, `_weekly_realized_pnl: float`
- `_last_day: date`, `_last_week_monday: date` — lazy reset triggers
- `daily_summary() -> tuple[int, int, float]` — public property for EOD report
- `weekly_summary() -> tuple[int, int, float]` — public property for weekly report
- Counters reset lazily on first trade of each new day/week

### `trading/position_monitor.py`

**Removed:** Alpaca order history queries, PDT guard checks, `TradingClient` import

**Changed dependency:** `TradingClient` → `TradierClient`

**`_check_positions()` flow:**
1. `get_all_positions()` → list of positions
2. `get_quotes(symbols)` → batch price fetch (one call for all open positions)
3. Compute `pnl_pct` using existing pure `compute_pnl_pct()` function
4. Fire SL/TP via `executor.sell()` — no PDT guard
5. Confirm closed for tickers in `pending_close` not returned by Tradier

**`_fetch_eod_data()` → reads `executor.daily_summary()`** — no network call

**`_fetch_weekly_data()` → reads `executor.weekly_summary()`** — no network call

### `news/news_handler.py`

Only change: `TradingClient.get_clock()` → `TradierClient.get_clock()` for market-open check. `NewsDataStream` unchanged.

### `main.py`

- Remove `_make_trading_client()`, `_load_open_positions()`
- Add `_make_tradier_client(config) -> TradierClient`
- Startup: call `tradier_client.get_all_positions()` to seed `held_tickers` / `shorted_tickers` only (no open_dates)
- Pass `TradierClient` to `OrderExecutor`, `PositionMonitor`, `NewsHandler`

### `.env.example`

Replace `ALPACA_BASE_URL` with:
```
TRADIER_ACCESS_TOKEN=your_tradier_access_token
TRADIER_ACCOUNT_ID=your_tradier_account_id
TRADIER_PAPER=true
```

## Testing

- Existing pure-function tests (`test_llm_advisor.py`, `test_position_monitor.py`) remain valid
- `test_order_executor.py` updated to remove PDT-related test cases
- `TradierClient` methods are sync and can be tested with `httpx` mock responses
- End-to-end testing against Tradier sandbox (`TRADIER_PAPER=true`)
