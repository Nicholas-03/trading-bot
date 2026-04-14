# Shared TradingClient & PDT Seed on Restart

**Date:** 2026-04-14

## Overview

Two independent fixes:

1. **Shared TradingClient** — eliminate three identical `TradingClient` instantiations and replace them with a single instance created in `main.py` and injected into all consumers.
2. **PDT seed on restart** — seed `OrderExecutor._open_dates` at startup by querying today's filled orders from Alpaca, so the PDT guard is not silently bypassed after a mid-day bot restart.

---

## Problem 1: Shared TradingClient

### Current state

`TradingClient` is constructed three times with identical credentials:

| Location | Attribute |
|---|---|
| `trading/order_executor.py:15` | `self._client` |
| `trading/position_monitor.py:16` | `self._client` |
| `news/news_handler.py:17` | `self._trading_client` |

`main.py/_load_open_positions()` also builds a fourth, local instance.

### Design

Create a single `TradingClient` in `main.py` and inject it into all three classes.

**`main.py`**
- Add `_make_trading_client(config: Config) -> TradingClient` helper.
- `_load_open_positions` accepts `client: TradingClient` instead of building its own.
- In `main()`, call `_make_trading_client(config)` once and pass the result to `_load_open_positions`, `OrderExecutor`, `PositionMonitor`, and `NewsHandler`.

**`trading/order_executor.py`**
- `__init__` signature: add `client: TradingClient` parameter; remove internal construction.

**`trading/position_monitor.py`**
- `__init__` signature: add `client: TradingClient` parameter; remove internal construction.

**`news/news_handler.py`**
- `__init__` signature: add `client: TradingClient` parameter (replaces `config`-based construction); remove internal construction.

**`tests/test_order_executor.py`**
- `_make_executor()`: pass `MagicMock()` as the `client` argument directly; remove `patch("trading.order_executor.TradingClient")`.

---

## Problem 2: PDT Seed on Restart

### Current state

`OrderExecutor._open_dates` is populated only when `buy()` or `short()` is called in the current process. A mid-day restart empties the dict, causing `is_opened_today()` to return `False` for positions that were opened earlier today — silently bypassing the PDT guard.

### Design

Extend `_load_open_positions()` to query today's filled orders and return a seeded `open_dates` dict. `OrderExecutor` accepts it as a constructor parameter.

**`main.py / _load_open_positions()`**
- After fetching open positions, call:
  ```python
  from alpaca.trading.requests import GetOrdersRequest
  from alpaca.trading.enums import QueryOrderStatus
  import pytz
  from datetime import datetime, date

  et = pytz.timezone("America/New_York")
  today_start = et.localize(datetime.combine(date.today(), datetime.min.time()))
  orders = client.get_orders(GetOrdersRequest(
      status=QueryOrderStatus.FILLED,
      after=today_start,
  ))
  open_today = {o.symbol for o in orders if o.symbol in (held | shorted)}
  open_dates = {symbol: date.today() for symbol in open_today}
  ```
- Return `(held, shorted, open_dates)` instead of `(held, shorted)`.
- Log seeded tickers at INFO level.

**`trading/order_executor.py`**
- `__init__` signature: add `open_dates: dict[str, date] | None = None` parameter.
- Assign `self._open_dates = dict(open_dates) if open_dates else {}` (avoids mutable default).

**`main.py / main()`**
- Unpack `held_tickers, shorted_tickers, open_dates = _load_open_positions(client, config)`.
- Pass `open_dates=open_dates` to `OrderExecutor(...)`.

**`tests/test_order_executor.py`**
- `_make_executor()` gains an optional `open_dates: dict[str, date] | None = None` kwarg.
- Existing tests are unchanged.

---

## Files Changed

| File | Change |
|---|---|
| `main.py` | Add `_make_trading_client`; update `_load_open_positions` signature + return; wire everything in `main()` |
| `trading/order_executor.py` | Accept `client` and `open_dates` params; remove internal `TradingClient` construction |
| `trading/position_monitor.py` | Accept `client` param; remove internal `TradingClient` construction |
| `news/news_handler.py` | Accept `client` param; remove internal `TradingClient` construction |
| `tests/test_order_executor.py` | Update `_make_executor()` to pass mock client directly |

## Out of Scope

- Caching the clock response in `NewsHandler`
- Persisting `open_dates` to disk across restarts beyond same-calendar-day seeding
- Changes to `PositionMonitor` or `NewsHandler` tests (no relevant new logic)
