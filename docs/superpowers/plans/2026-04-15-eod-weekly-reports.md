# EOD & Weekly Telegram Reports Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send a trade-summary Telegram report at 4:00 PM ET every trading day, and an additional weekly summary on Fridays.

**Architecture:** Extend `PositionMonitor` with a `_report_loop()` coroutine that checks the ET clock each minute. At 4:00 PM it fetches filled-order counts and portfolio P&L via the Alpaca API, then dispatches to two new notifier methods. `TelegramNotifier`, its protocol, and `NoOpNotifier` all gain `notify_eod_report` and `notify_weekly_report`.

**Tech Stack:** Python asyncio, alpaca-py `TradingClient` (`get_orders`, `get_portfolio_history`), pytz, pytest

---

## File Map

| File | Change |
|------|--------|
| `notifications/telegram_notifier.py` | Add `notify_eod_report` / `notify_weekly_report` to `Notifier` protocol, `TelegramNotifier`, `NoOpNotifier` |
| `trading/position_monitor.py` | Add `notifier` param; extract `_position_loop()`; add `_should_fire_report()`, `_report_loop()`, `_fetch_eod_data()`, `_fetch_weekly_data()` |
| `main.py` | Pass `notifier` to `PositionMonitor` constructor |
| `tests/test_position_monitor.py` | Add tests for `_should_fire_report`; update `_make_monitor` helper to pass a notifier mock |

---

### Task 1: Add EOD/weekly notify methods to `TelegramNotifier`

**Files:**
- Modify: `notifications/telegram_notifier.py`

- [ ] **Step 1: Add the two methods to the `Notifier` protocol**

In `notifications/telegram_notifier.py`, extend the `Notifier` protocol (lines 11–16):

```python
@runtime_checkable
class Notifier(Protocol):
    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None: ...
    async def notify_sell(self, ticker: str, pnl_pct: float | None = None, pnl_usd: float | None = None) -> None: ...
    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None: ...
    async def notify_error(self, action: str, detail: str) -> None: ...
    async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None: ...
    async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None: ...
    async def aclose(self) -> None: ...
```

- [ ] **Step 2: Add the public methods and formatters to `TelegramNotifier`**

Add `import pytz` at the top of the file and add `from datetime import datetime` (it isn't imported yet).

After `notify_error` (around line 39), add:

```python
async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
    await self._send(self._format_eod_report(buys, sells, pnl))

async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
    await self._send(self._format_weekly_report(buys, sells, pnl))
```

After `_format_error`, add the two formatters:

```python
def _format_eod_report(self, buys: int, sells: int, pnl: float) -> str:
    et = pytz.timezone("America/New_York")
    today = datetime.now(et)
    day_str = f"{today.strftime('%a %b')} {today.day}"
    sign = "+" if pnl >= 0 else ""
    return (
        f"📊 End of Day Report — {day_str}\n"
        f"🟢 Buys: {buys}\n"
        f"🔴 Sells: {sells}\n"
        f"💰 Realized P&L: {sign}${pnl:.2f}"
    )

def _format_weekly_report(self, buys: int, sells: int, pnl: float) -> str:
    et = pytz.timezone("America/New_York")
    today = datetime.now(et)
    day_str = f"{today.strftime('%b')} {today.day}"
    sign = "+" if pnl >= 0 else ""
    return (
        f"📅 Weekly Report — Week of {day_str}\n"
        f"🟢 Buys: {buys}\n"
        f"🔴 Sells: {sells}\n"
        f"💰 Realized P&L: {sign}${pnl:.2f}"
    )
```

- [ ] **Step 3: Add the stub methods to `NoOpNotifier`**

In `NoOpNotifier` (at the bottom of the file), add after `notify_error`:

```python
async def notify_eod_report(self, buys: int, sells: int, pnl: float) -> None:
    pass

async def notify_weekly_report(self, buys: int, sells: int, pnl: float) -> None:
    pass
```

- [ ] **Step 4: Verify imports compile cleanly**

```bash
python -c "from notifications.telegram_notifier import TelegramNotifier, NoOpNotifier, Notifier; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add notifications/telegram_notifier.py
git commit -m "feat: add notify_eod_report and notify_weekly_report to TelegramNotifier"
```

---

### Task 2: Add `_should_fire_report` pure function with tests (TDD)

**Files:**
- Modify: `tests/test_position_monitor.py`
- Modify: `trading/position_monitor.py`

- [ ] **Step 1: Write the failing tests**

Add to the bottom of `tests/test_position_monitor.py`:

```python
import pytz
from datetime import date, datetime
from trading.position_monitor import _should_fire_report

_ET = pytz.timezone("America/New_York")


def test_should_fire_at_market_close_no_prior_report():
    now = _ET.localize(datetime(2026, 4, 14, 16, 0, 30))
    assert _should_fire_report(now, None) is True


def test_should_not_fire_already_fired_today():
    now = _ET.localize(datetime(2026, 4, 14, 16, 0, 30))
    assert _should_fire_report(now, date(2026, 4, 14)) is False


def test_should_not_fire_before_close():
    now = _ET.localize(datetime(2026, 4, 14, 15, 59, 59))
    assert _should_fire_report(now, None) is False


def test_should_not_fire_after_close_minute():
    now = _ET.localize(datetime(2026, 4, 14, 16, 1, 0))
    assert _should_fire_report(now, None) is False


def test_should_fire_new_day_after_previous_report():
    now = _ET.localize(datetime(2026, 4, 15, 16, 0, 0))
    assert _should_fire_report(now, date(2026, 4, 14)) is True
```

- [ ] **Step 2: Run the tests to confirm they fail**

```bash
python -m pytest tests/test_position_monitor.py::test_should_fire_at_market_close_no_prior_report -v
```

Expected: `ImportError` or `FAILED` — `_should_fire_report` doesn't exist yet.

- [ ] **Step 3: Add `_should_fire_report` to `position_monitor.py`**

Add these imports at the top of `trading/position_monitor.py`:

```python
from datetime import date, datetime, timedelta
import pytz
```

Add the pure function directly below the imports (before the `logger` line is fine, or after `compute_pnl_pct`):

```python
def _should_fire_report(now_et: datetime, last_report_date: date | None) -> bool:
    """Return True if the EOD/weekly report should fire now.

    Fires during the 16:00:00–16:00:59 ET window, at most once per calendar day.
    """
    if now_et.hour != 16 or now_et.minute != 0:
        return False
    return last_report_date != now_et.date()
```

- [ ] **Step 4: Run the tests to confirm they pass**

```bash
python -m pytest tests/test_position_monitor.py -v
```

Expected: all tests pass (existing + 5 new).

- [ ] **Step 5: Commit**

```bash
git add trading/position_monitor.py tests/test_position_monitor.py
git commit -m "feat: add _should_fire_report pure function with tests"
```

---

### Task 3: Refactor `PositionMonitor` and wire `main.py`

**Files:**
- Modify: `trading/position_monitor.py`
- Modify: `tests/test_position_monitor.py` (update `_make_monitor` helper)
- Modify: `main.py`

- [ ] **Step 1: Add remaining imports to `position_monitor.py`**

The full import block for `trading/position_monitor.py` should now be:

```python
import asyncio
import logging
from datetime import date, datetime, timedelta
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest, GetPortfolioHistoryRequest
from alpaca.trading.enums import QueryOrderStatus, OrderStatus, OrderSide
from trading.order_executor import OrderExecutor
from notifications.telegram_notifier import Notifier
from config import Config
```

- [ ] **Step 2: Update `PositionMonitor.__init__` to accept `notifier`**

Replace the existing `__init__` and `run` method with the full updated class body. The complete `PositionMonitor` class becomes:

```python
class PositionMonitor:
    def __init__(
        self,
        client: TradingClient,
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
        buys, sells, pnl = await asyncio.to_thread(self._fetch_eod_data)
        await self._notifier.notify_eod_report(buys, sells, pnl)
        self._last_report_date = today
        logger.info("EOD report sent: buys=%d sells=%d pnl=%.2f", buys, sells, pnl)

        if today.weekday() == 4:  # Friday
            w_buys, w_sells, w_pnl = await asyncio.to_thread(self._fetch_weekly_data)
            await self._notifier.notify_weekly_report(w_buys, w_sells, w_pnl)
            logger.info("Weekly report sent: buys=%d sells=%d pnl=%.2f", w_buys, w_sells, w_pnl)

    def _fetch_eod_data(self) -> tuple[int, int, float]:
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        today_start = et.localize(datetime.combine(today, datetime.min.time()))

        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start, limit=500)
        )
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        buys = sum(1 for o in filled if o.side == OrderSide.BUY)
        sells = sum(1 for o in filled if o.side == OrderSide.SELL)

        history = self._client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1D")
        )
        pnl = sum(v for v in (history.profit_loss or []) if v is not None)

        return buys, sells, pnl

    def _fetch_weekly_data(self) -> tuple[int, int, float]:
        et = pytz.timezone("America/New_York")
        today = datetime.now(et).date()
        week_start = today - timedelta(days=today.weekday())  # Monday
        week_start_dt = et.localize(datetime.combine(week_start, datetime.min.time()))

        orders = self._client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=week_start_dt, limit=500)
        )
        filled = [o for o in orders if o.status == OrderStatus.FILLED]
        buys = sum(1 for o in filled if o.side == OrderSide.BUY)
        sells = sum(1 for o in filled if o.side == OrderSide.SELL)

        history = self._client.get_portfolio_history(
            GetPortfolioHistoryRequest(period="1W")
        )
        pnl = sum(v for v in (history.profit_loss or []) if v is not None)

        return buys, sells, pnl

    async def _check_positions(self) -> None:
        positions = self._client.get_all_positions()
        for pos in positions:
            try:
                ticker = pos.symbol
                entry = float(pos.avg_entry_price)
                if entry == 0.0:
                    logger.warning("Skipping %s — avg_entry_price is zero", ticker)
                    continue
                current = float(pos.current_price)
                pnl = compute_pnl_pct(entry, current)

                pnl_usd = float(pos.unrealized_pl)
                if pnl <= -self._stop_loss:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping stop-loss close for %s (opened today)", ticker)
                    else:
                        logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
                elif pnl >= self._take_profit:
                    if self._executor.is_opened_today(ticker):
                        logger.info("PDT guard — skipping take-profit close for %s (opened today)", ticker)
                    else:
                        logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                        await self._executor.sell(ticker, pnl_pct=pnl, pnl_usd=pnl_usd)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
```

- [ ] **Step 3: Update `_make_monitor` in the test file**

In `tests/test_position_monitor.py`, update `_make_monitor` to pass a notifier mock:

```python
def _make_monitor(stop_loss=0.02, take_profit=0.03):
    config = MagicMock()
    config.alpaca_api_key = "key"
    config.alpaca_secret_key = "secret"
    config.paper = True
    config.stop_loss_pct = stop_loss
    config.take_profit_pct = take_profit

    executor = MagicMock()
    executor.sell = AsyncMock()

    notifier = MagicMock()
    notifier.notify_eod_report = AsyncMock()
    notifier.notify_weekly_report = AsyncMock()

    client = MagicMock()
    monitor = PositionMonitor(client, config, executor, notifier)

    return monitor, executor
```

- [ ] **Step 4: Update `main.py` to pass `notifier` to `PositionMonitor`**

Change line 79 in `main.py`:

```python
position_monitor = PositionMonitor(client, config, order_executor, notifier)
```

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 6: Verify imports compile cleanly**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 7: Commit**

```bash
git add trading/position_monitor.py tests/test_position_monitor.py main.py
git commit -m "feat: add EOD and weekly report loop to PositionMonitor"
```
