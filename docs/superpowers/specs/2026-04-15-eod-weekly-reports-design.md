# EOD & Weekly Telegram Reports — Design Spec

**Date:** 2026-04-15

## Overview

Send a trade-summary Telegram report at market close (4:00 PM ET) every trading day, and an additional weekly summary on Fridays. Reports include trade counts (buys/sells) and realized P&L for the period.

## Architecture

### Trigger mechanism

`PositionMonitor` gains a second internal coroutine, `_report_loop()`, run alongside the existing position-check loop via `asyncio.gather` inside `PositionMonitor.run()`. The existing `while True` body moves into `_position_loop()` unchanged.

`_report_loop()` sleeps 60 seconds per tick. On each wake it:
1. Gets the current ET datetime.
2. Checks if the time is in the 16:00–16:01 window (one-minute firing window to avoid missing the exact second).
3. Checks `_last_report_date != today` to fire at most once per calendar day.
4. If both conditions met: fetches data, sends EOD report, updates `_last_report_date`.
5. If today is Friday: also sends the weekly report immediately after.

No Alpaca market-clock API calls — hardcoded 16:00 ET matches the approach used for the existing market-hours guard.

### Data sources

- **Trade counts** — `client.get_orders(GetOrdersRequest(status=CLOSED, after=<period_start>))` filtered to `OrderStatus.FILLED`. Count `OrderSide.BUY` for buys, `OrderSide.SELL` for sells.
- **Realized P&L** — `client.get_portfolio_history(period="1D")` for EOD (returns `profit_loss` list; sum the values). For the weekly report, `period="1W"`.

### `PositionMonitor` changes

| Change | Detail |
|--------|--------|
| Constructor | Gains `notifier: Notifier` parameter |
| New field | `_last_report_date: date \| None = None` |
| `run()` | Becomes `await asyncio.gather(self._position_loop(), self._report_loop())` |
| `_position_loop()` | Existing `while True` body, moved verbatim |
| `_report_loop()` | New — 60s sleep, clock check, report dispatch |

### `TelegramNotifier` changes

Two new methods added to the `Notifier` protocol and implemented in `TelegramNotifier` and `NoOpNotifier`:

- `notify_eod_report(buys: int, sells: int, pnl: float) -> None`
- `notify_weekly_report(buys: int, sells: int, pnl: float) -> None`

### `main.py` changes

Pass `notifier` to `PositionMonitor` constructor.

## Message Format

**EOD report:**
```
📊 End of Day Report — Mon Apr 14
🟢 Buys: 3
🔴 Sells: 2
💰 Realized P&L: +$1.23
```

**Weekly report (Fridays only, fires immediately after EOD):**
```
📅 Weekly Report — Week of Apr 14
🟢 Buys: 12
🔴 Sells: 9
💰 Realized P&L: +$4.56
```

P&L sign prefix: `+` if ≥ 0, `-` if negative (already natural from float formatting).

## Error Handling

- If `get_portfolio_history` or `get_orders` raises, log the exception and skip the report for that day (do not retry — next day's report is independent).
- `_last_report_date` is only updated on successful dispatch to avoid silently swallowing a failure.

## Testing

No new unit tests required — the pure logic here is trivial clock arithmetic. End-to-end verified manually against paper trading at 4:00 PM ET.

## Out of Scope

- Early market closes (e.g., day before Thanksgiving) — hardcoded 16:00 ET is acceptable.
- Sending the report if the bot was not running at 4:00 PM (no catch-up on restart).
- Per-trade breakdown in the report (deferred by design — Option A chosen).
