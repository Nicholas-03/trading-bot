# Telegram Notifications — Design Spec

**Date:** 2026-04-10

## Overview

Send formatted Telegram messages for trade executions (buy, sell, short) and errors to a user-configured Telegram bot. Controlled by a toggle in `.env`.

## Architecture

A new `notifications/telegram_notifier.py` module exposes two classes:

- `TelegramNotifier` — sends real messages via Telegram Bot API
- `NoOpNotifier` — drop-in stub used when `TELEGRAM_ENABLED=false`; all methods do nothing

`TelegramNotifier` is constructed in `main.py` alongside `Config` and injected into `OrderExecutor`. Using a no-op stub means no `if notifier:` guards are needed anywhere in the codebase.

Messages are sent via async HTTP POST to `https://api.telegram.org/bot{token}/sendMessage` using `httpx`. Calls are fire-and-forget: failures are logged locally but never raised, so a Telegram outage cannot crash the trading loop.

## Config

Three new `.env` variables:

```
TELEGRAM_ENABLED=true
TELEGRAM_BOT_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```

`TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are only required when `TELEGRAM_ENABLED=true`, consistent with how `ANTHROPIC_API_KEY` is only required when `LLM_PROVIDER=claude`. Added to the frozen `Config` dataclass in `config.py`.

## TelegramNotifier Interface

```python
class TelegramNotifier:
    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None: ...
    async def notify_sell(self, ticker: str) -> None: ...
    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None: ...
    async def notify_error(self, action: str, detail: str) -> None: ...

class NoOpNotifier(TelegramNotifier):
    """Used when TELEGRAM_ENABLED=false — all methods do nothing."""
```

`OrderExecutor.buy`, `sell`, and `short` become `async` methods. The notifier is `await`ed inside each method after a successful order submission or on error. `OrderExecutor` is called directly from the async `NewsHandler` coroutine, so making its methods async is a clean change with no `asyncio.to_thread` wrapping needed.

## Message Formats

**Buy:**
```
✅ BUY executed
📌 Ticker: AAPL
💵 Notional: $5.00
🔖 Order ID: abc123
```

**Sell:**
```
🔴 SELL executed
📌 Ticker: AAPL
```

**Short:**
```
🩳 SHORT executed
📌 Ticker: AAPL
🔢 Qty: 1
🔖 Order ID: abc123
```

**Error:**
```
❌ ERROR in OrderExecutor
📌 Action: buy AAPL
⚠️ Detail: <error message>
```

## Error Handling

The internal `_send()` method wraps all HTTP calls in a try/except. Any failure (network error, bad token, rate limit) is logged via `logger.warning()` and silently dropped. The trading loop is never interrupted by a notification failure.

## Testing

New file: `tests/test_telegram_notifier.py`

- `test_noop_notifier_does_nothing` — call all `NoOpNotifier` methods, assert no HTTP calls made
- `test_message_format` — assert message strings are built correctly for each event type (pure string logic, no HTTP)

The HTTP call is isolated in `_send()`, keeping all public methods' formatting logic fully testable without network access.

## Files Changed

| File | Change |
|------|--------|
| `notifications/telegram_notifier.py` | New — `TelegramNotifier` and `NoOpNotifier` |
| `config.py` | Add `telegram_enabled`, `telegram_bot_token`, `telegram_chat_id` |
| `.env.example` | Add `TELEGRAM_ENABLED`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` |
| `trading/order_executor.py` | Inject notifier; make `buy`/`sell`/`short` async; add notify calls |
| `main.py` | Construct notifier; pass to `OrderExecutor` |
| `requirements.txt` | Add `httpx` |
| `tests/test_telegram_notifier.py` | New — unit tests |
