# Telegram Notifications Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send formatted Telegram messages for trade executions (buy/sell/short) and errors via a `TelegramNotifier` injected into `OrderExecutor`.

**Architecture:** A new `notifications/telegram_notifier.py` module exposes `TelegramNotifier` (sends real messages via `httpx`) and `NoOpNotifier` (no-op stub). `OrderExecutor.buy/sell/short` become async and call the notifier. All call sites are updated to `await` them.

**Tech Stack:** Python 3.11+, `httpx>=0.27.0` (async HTTP), `unittest.mock.AsyncMock` (tests)

---

## File Map

| Action | Path | Responsibility |
|--------|------|---------------|
| Create | `notifications/__init__.py` | Package marker |
| Create | `notifications/telegram_notifier.py` | `TelegramNotifier`, `NoOpNotifier` |
| Create | `tests/test_telegram_notifier.py` | Unit tests for notifier |
| Modify | `requirements.txt` | Add `httpx>=0.27.0` |
| Modify | `config.py` | Add `telegram_enabled`, `telegram_bot_token`, `telegram_chat_id` |
| Modify | `.env.example` | Document new vars |
| Modify | `trading/order_executor.py` | Inject notifier; make methods async; add notify calls |
| Modify | `news/news_handler.py` | `await` executor calls |
| Modify | `trading/position_monitor.py` | Make `_check_positions` async; `await` executor calls |
| Modify | `main.py` | Construct notifier; pass to `OrderExecutor`; `aclose()` on shutdown |

---

## Task 1: Add httpx and create notifications package

**Files:**
- Modify: `requirements.txt`
- Create: `notifications/__init__.py`

- [ ] **Step 1: Add httpx to requirements.txt**

Open `requirements.txt` and add after the last line:
```
httpx>=0.27.0
```

- [ ] **Step 2: Create the notifications package**

Create `notifications/__init__.py` with empty content:
```python
```

- [ ] **Step 3: Verify import works**

Run:
```bash
pip install -r requirements.txt
python -c "import httpx; import notifications; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add requirements.txt notifications/__init__.py
git commit -m "chore: add httpx and notifications package"
```

---

## Task 2: Implement TelegramNotifier and NoOpNotifier

**Files:**
- Create: `notifications/telegram_notifier.py`
- Create: `tests/test_telegram_notifier.py`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_telegram_notifier.py`:
```python
import asyncio
import pytest
from unittest.mock import AsyncMock, patch
from notifications.telegram_notifier import TelegramNotifier, NoOpNotifier


# --- NoOpNotifier ---

def test_noop_notifier_does_nothing():
    notifier = NoOpNotifier()
    # All methods must be awaitable and produce no side effects
    asyncio.get_event_loop().run_until_complete(notifier.notify_buy("AAPL", 5.0, "order-1"))
    asyncio.get_event_loop().run_until_complete(notifier.notify_sell("AAPL"))
    asyncio.get_event_loop().run_until_complete(notifier.notify_short("AAPL", 1, "order-2"))
    asyncio.get_event_loop().run_until_complete(notifier.notify_error("buy AAPL", "some error"))


# --- TelegramNotifier message formatting ---

def test_format_buy():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_buy("AAPL", 5.0, "abc123")
    assert "✅ BUY executed" in msg
    assert "AAPL" in msg
    assert "$5.00" in msg
    assert "abc123" in msg


def test_format_sell():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_sell("AAPL")
    assert "🔴 SELL executed" in msg
    assert "AAPL" in msg


def test_format_short():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_short("TSLA", 2, "xyz789")
    assert "🩳 SHORT executed" in msg
    assert "TSLA" in msg
    assert "2" in msg
    assert "xyz789" in msg


def test_format_error():
    n = TelegramNotifier.__new__(TelegramNotifier)
    msg = n._format_error("buy AAPL", "Connection refused")
    assert "❌ ERROR" in msg
    assert "buy AAPL" in msg
    assert "Connection refused" in msg


def test_notify_buy_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.get_event_loop().run_until_complete(n.notify_buy("AAPL", 5.0, "abc123"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "AAPL" in message
    assert "$5.00" in message


def test_notify_sell_calls_send_with_formatted_message():
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._send = AsyncMock()
    asyncio.get_event_loop().run_until_complete(n.notify_sell("MSFT"))
    n._send.assert_called_once()
    message = n._send.call_args[0][0]
    assert "MSFT" in message


def test_send_failure_does_not_raise():
    """A Telegram API failure must never propagate to the caller."""
    import httpx
    n = TelegramNotifier.__new__(TelegramNotifier)
    n._token = "tok"
    n._chat_id = "123"
    n._client = AsyncMock()
    n._client.post = AsyncMock(side_effect=httpx.ConnectError("unreachable"))
    # Should not raise
    asyncio.get_event_loop().run_until_complete(n._send("hello"))
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_telegram_notifier.py -v
```
Expected: errors like `ModuleNotFoundError: No module named 'notifications.telegram_notifier'`

- [ ] **Step 3: Implement notifications/telegram_notifier.py**

Create `notifications/telegram_notifier.py`:
```python
import logging
import httpx

logger = logging.getLogger(__name__)

_API_URL = "https://api.telegram.org/bot{token}/sendMessage"


class TelegramNotifier:
    def __init__(self, token: str, chat_id: str) -> None:
        self._token = token
        self._chat_id = chat_id
        self._client = httpx.AsyncClient(timeout=10.0)

    # --- Public notify methods ---

    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None:
        await self._send(self._format_buy(ticker, notional, order_id))

    async def notify_sell(self, ticker: str) -> None:
        await self._send(self._format_sell(ticker))

    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None:
        await self._send(self._format_short(ticker, qty, order_id))

    async def notify_error(self, action: str, detail: str) -> None:
        await self._send(self._format_error(action, detail))

    async def aclose(self) -> None:
        await self._client.aclose()

    # --- Message formatters ---

    def _format_buy(self, ticker: str, notional: float, order_id: str) -> str:
        return (
            f"✅ BUY executed\n"
            f"📌 Ticker: {ticker}\n"
            f"💵 Notional: ${notional:.2f}\n"
            f"🔖 Order ID: {order_id}"
        )

    def _format_sell(self, ticker: str) -> str:
        return (
            f"🔴 SELL executed\n"
            f"📌 Ticker: {ticker}"
        )

    def _format_short(self, ticker: str, qty: int, order_id: str) -> str:
        return (
            f"🩳 SHORT executed\n"
            f"📌 Ticker: {ticker}\n"
            f"🔢 Qty: {qty}\n"
            f"🔖 Order ID: {order_id}"
        )

    def _format_error(self, action: str, detail: str) -> str:
        return (
            f"❌ ERROR in OrderExecutor\n"
            f"📌 Action: {action}\n"
            f"⚠️ Detail: {detail}"
        )

    # --- HTTP transport ---

    async def _send(self, message: str) -> None:
        try:
            await self._client.post(
                _API_URL.format(token=self._token),
                json={"chat_id": self._chat_id, "text": message},
            )
        except Exception as e:
            logger.warning("Telegram notification failed: %s", e)


class NoOpNotifier(TelegramNotifier):
    """Drop-in stub used when TELEGRAM_ENABLED=false."""

    def __init__(self) -> None:
        pass  # No HTTP client needed

    async def notify_buy(self, ticker: str, notional: float, order_id: str) -> None:
        pass

    async def notify_sell(self, ticker: str) -> None:
        pass

    async def notify_short(self, ticker: str, qty: int, order_id: str) -> None:
        pass

    async def notify_error(self, action: str, detail: str) -> None:
        pass

    async def aclose(self) -> None:
        pass
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_telegram_notifier.py -v
```
Expected: all tests PASS

- [ ] **Step 5: Commit**

```bash
git add notifications/telegram_notifier.py tests/test_telegram_notifier.py
git commit -m "feat: add TelegramNotifier and NoOpNotifier with tests"
```

---

## Task 3: Update Config with Telegram fields

**Files:**
- Modify: `config.py`
- Modify: `.env.example`

- [ ] **Step 1: Add fields to Config dataclass**

In `config.py`, add three fields to the `Config` dataclass after `take_profit_pct`:
```python
    telegram_enabled: bool
    telegram_bot_token: str
    telegram_chat_id: str
```

- [ ] **Step 2: Add parsing and validation in load_config()**

In `load_config()`, add validation after the existing required-keys check:
```python
    telegram_enabled = os.getenv("TELEGRAM_ENABLED", "false").lower() in ("true", "1", "yes")
    if telegram_enabled:
        telegram_missing = [k for k in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") if not os.getenv(k)]
        if telegram_missing:
            raise ValueError(f"TELEGRAM_ENABLED=true but missing: {', '.join(telegram_missing)}")
```

Then in the `Config(...)` constructor call, add:
```python
        telegram_enabled=telegram_enabled,
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
```

- [ ] **Step 3: Update .env.example**

Add at the end of `.env.example`:
```
# Telegram notifications
TELEGRAM_ENABLED=false
TELEGRAM_BOT_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
```

- [ ] **Step 4: Verify config loads cleanly**

```bash
python -c "from config import load_config; print('OK')"
```
Expected: `OK` (uses existing `.env` which won't have the new vars, so defaults to disabled)

- [ ] **Step 5: Commit**

```bash
git add config.py .env.example
git commit -m "feat: add Telegram config fields"
```

---

## Task 4: Update OrderExecutor — inject notifier, make methods async

**Files:**
- Modify: `trading/order_executor.py`

- [ ] **Step 1: Update imports and constructor**

At the top of `trading/order_executor.py`, add the import:
```python
from notifications.telegram_notifier import TelegramNotifier
```

Update the `__init__` signature to accept the notifier:
```python
    def __init__(self, config: Config, held_tickers: set[str], shorted_tickers: set[str], notifier: TelegramNotifier) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
```

- [ ] **Step 2: Make buy() async and add notify calls**

Replace the existing `buy()` method:
```python
    async def buy(self, ticker: str) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        if ticker in self._shorted_tickers:
            logger.info("Skipping buy for %s — currently shorted, cover first", ticker)
            return
        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    notional=self._notional_usd,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._held_tickers.add(ticker)
            logger.info(
                "BUY order accepted for %s $%.2f — order %s (pending fill)",
                ticker, self._notional_usd, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_buy(ticker, self._notional_usd, str(getattr(order, "id", "unknown")))
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)
            await self._notifier.notify_error(f"buy {ticker}", str(e))
```

- [ ] **Step 3: Make short() async and add notify calls**

Replace the existing `short()` method:
```python
    async def short(self, ticker: str) -> None:
        if ticker in self._shorted_tickers:
            logger.info("Skipping short for %s — already shorted", ticker)
            return
        if ticker in self._held_tickers:
            logger.info("Skipping short for %s — currently held long, sell first", ticker)
            return
        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    qty=self._short_qty,
                    side=OrderSide.SELL,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._shorted_tickers.add(ticker)
            logger.info(
                "SHORT order accepted for %s qty=%d — order %s (pending fill)",
                ticker, self._short_qty, getattr(order, "id", "unknown"),
            )
            await self._notifier.notify_short(ticker, self._short_qty, str(getattr(order, "id", "unknown")))
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)
            await self._notifier.notify_error(f"short {ticker}", str(e))
```

- [ ] **Step 4: Make sell() async and add notify calls**

Replace the existing `sell()` method:
```python
    async def sell(self, ticker: str) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            self._shorted_tickers.discard(ticker)
            logger.info("CLOSED position for %s", ticker)
            await self._notifier.notify_sell(ticker)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status in (404, 422):
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                logger.warning("Close %s — position not found (status %s), removing from tracking", ticker, status)
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
                await self._notifier.notify_error(f"sell {ticker}", str(e))
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
            await self._notifier.notify_error(f"sell {ticker}", str(e))
```

- [ ] **Step 5: Verify the file imports cleanly**

```bash
python -c "from trading.order_executor import OrderExecutor; print('OK')"
```
Expected: `OK`

- [ ] **Step 6: Commit**

```bash
git add trading/order_executor.py
git commit -m "feat: make OrderExecutor methods async and add notifier calls"
```

---

## Task 5: Update call sites — news_handler and position_monitor

**Files:**
- Modify: `news/news_handler.py`
- Modify: `trading/position_monitor.py`

- [ ] **Step 1: Await executor calls in news_handler.py**

In `news/news_handler.py`, update the three executor calls in `_handle_news()`:
```python
            if decision.action == "buy" and decision.ticker:
                await self._executor.buy(decision.ticker)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(decision.ticker)
                else:
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
```

- [ ] **Step 2: Make _check_positions async in position_monitor.py**

In `trading/position_monitor.py`, update `run()` to await `_check_positions`:
```python
    async def run(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")
```

Change `_check_positions` to `async def` and await the sell call:
```python
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

                if pnl <= -self._stop_loss:
                    logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker)
                elif pnl >= self._take_profit:
                    logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    await self._executor.sell(ticker)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
```

- [ ] **Step 3: Verify both modules import cleanly**

```bash
python -c "from news.news_handler import NewsHandler; from trading.position_monitor import PositionMonitor; print('OK')"
```
Expected: `OK`

- [ ] **Step 4: Commit**

```bash
git add news/news_handler.py trading/position_monitor.py
git commit -m "feat: await async OrderExecutor methods in handler and monitor"
```

---

## Task 6: Wire notifier in main.py

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Add notifier import**

At the top of `main.py`, add:
```python
from notifications.telegram_notifier import TelegramNotifier, NoOpNotifier
```

- [ ] **Step 2: Construct notifier and pass to OrderExecutor**

In `main()`, after `config = load_config()`, add notifier construction:
```python
    if config.telegram_enabled:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        notifier = NoOpNotifier()
```

Update the `OrderExecutor` construction to pass `notifier`:
```python
    order_executor = OrderExecutor(config, held_tickers, shorted_tickers, notifier)
```

- [ ] **Step 3: Close notifier on shutdown**

Wrap the `asyncio.gather` call in a try/finally to close the notifier:
```python
    try:
        await asyncio.gather(
            news_handler.run(),
            position_monitor.run(),
        )
    except asyncio.CancelledError:
        logger.info("Bot shutting down")
    finally:
        await notifier.aclose()
```

- [ ] **Step 4: Verify full import chain**

```bash
python -c "import main; print('OK')"
```
Expected: `OK`

- [ ] **Step 5: Run all tests**

```bash
python -m pytest tests/ -v
```
Expected: all tests PASS

- [ ] **Step 6: Commit**

```bash
git add main.py
git commit -m "feat: wire TelegramNotifier into main entry point"
```
