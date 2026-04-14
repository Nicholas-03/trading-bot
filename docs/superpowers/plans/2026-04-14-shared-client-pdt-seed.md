# Shared TradingClient & PDT Seed on Restart Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate three redundant `TradingClient` instantiations by injecting one shared instance, and seed `OrderExecutor._open_dates` at startup from today's filled order history so the PDT guard survives a mid-day restart.

**Architecture:** A single `TradingClient` is created in `main.py` and passed into `OrderExecutor`, `PositionMonitor`, and `NewsHandler`. `_load_open_positions()` is extended to also query today's filled orders and return a seeded `open_dates` dict that `OrderExecutor` accepts in its constructor.

**Tech Stack:** Python 3.11+, alpaca-py (`TradingClient`, `GetOrdersRequest`, `QueryOrderStatus`), pytz (already in requirements.txt)

---

## File Map

| File | Change |
|---|---|
| `trading/order_executor.py` | Accept `client: TradingClient` and `open_dates: dict[str, date] \| None = None`; remove internal construction |
| `trading/position_monitor.py` | Accept `client: TradingClient`; remove internal construction |
| `news/news_handler.py` | Accept `client: TradingClient`; remove internal construction |
| `main.py` | Add `_make_trading_client`; extend `_load_open_positions`; wire `main()` |
| `tests/test_order_executor.py` | Pass mock client directly; add `open_dates` seeding test |

---

## Task 1: Update OrderExecutor to accept an injected client

**Files:**
- Modify: `trading/order_executor.py`
- Modify: `tests/test_order_executor.py`

- [ ] **Step 1: Update `_make_executor` in tests to pass a mock client directly**

Replace the entire `_make_executor` function in `tests/test_order_executor.py`:

```python
def _make_executor(open_dates: dict | None = None) -> OrderExecutor:
    config = MagicMock(spec=Config)
    config.trade_amount_usd = 5.0
    config.short_qty = 1
    notifier = MagicMock()
    notifier.notify_buy = AsyncMock()
    notifier.notify_short = AsyncMock()
    notifier.notify_sell = AsyncMock()
    notifier.notify_error = AsyncMock()
    client = MagicMock()
    return OrderExecutor(client, config, set(), set(), notifier, open_dates=open_dates)
```

Also remove the now-unused import at the top of the test file:
```python
from unittest.mock import MagicMock, AsyncMock
import pytest
from datetime import date, timedelta
from alpaca.common.exceptions import APIError
from trading.order_executor import OrderExecutor
from config import Config
```
(drop `patch` from the `unittest.mock` import)

- [ ] **Step 2: Run the tests to confirm they fail**

```
python -m pytest tests/test_order_executor.py -v
```

Expected: FAIL — `TypeError: OrderExecutor.__init__() got an unexpected keyword argument 'client'` or similar.

- [ ] **Step 3: Update `OrderExecutor.__init__` to accept `client`**

Replace the top of `trading/order_executor.py` (imports + `__init__`) with:

```python
import logging
from datetime import date
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from config import Config
from notifications.telegram_notifier import Notifier

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(
        self,
        client: TradingClient,
        config: Config,
        held_tickers: set[str],
        shorted_tickers: set[str],
        notifier: Notifier,
        open_dates: dict[str, date] | None = None,
    ) -> None:
        self._client = client
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers
        self._notifier = notifier
        self._open_dates: dict[str, date] = dict(open_dates) if open_dates else {}
```

- [ ] **Step 4: Run the tests to confirm they pass**

```
python -m pytest tests/test_order_executor.py -v
```

Expected: all 5 existing tests PASS.

- [ ] **Step 5: Commit**

```bash
git add trading/order_executor.py tests/test_order_executor.py
git commit -m "refactor: inject TradingClient into OrderExecutor; accept open_dates seed"
```

---

## Task 2: Add test for open_dates seeding via constructor

**Files:**
- Modify: `tests/test_order_executor.py`

- [ ] **Step 1: Write the failing test**

Add to the bottom of `tests/test_order_executor.py`:

```python
def test_open_dates_seeded_from_constructor():
    seeded = {"AAPL": date.today(), "TSLA": date.today() - timedelta(days=1)}
    ex = _make_executor(open_dates=seeded)
    assert ex.is_opened_today("AAPL") is True
    assert ex.is_opened_today("TSLA") is False  # yesterday
    assert ex.is_opened_today("MSFT") is False  # not seeded


def test_open_dates_constructor_copy_is_isolated():
    """Mutating the original dict must not affect the executor's tracking."""
    seeded = {"AAPL": date.today()}
    ex = _make_executor(open_dates=seeded)
    seeded["AAPL"] = date.today() - timedelta(days=1)  # mutate original
    assert ex.is_opened_today("AAPL") is True  # executor unaffected
```

- [ ] **Step 2: Run the tests to verify they pass (implementation already complete from Task 1)**

```
python -m pytest tests/test_order_executor.py -v
```

Expected: all 7 tests PASS.

- [ ] **Step 3: Commit**

```bash
git add tests/test_order_executor.py
git commit -m "test: verify open_dates seeding and isolation in OrderExecutor constructor"
```

---

## Task 3: Update PositionMonitor to accept an injected client

**Files:**
- Modify: `trading/position_monitor.py`

- [ ] **Step 1: Update `PositionMonitor.__init__`**

Replace the entire `PositionMonitor` class header in `trading/position_monitor.py`:

```python
import asyncio
import logging
from alpaca.trading.client import TradingClient
from trading.order_executor import OrderExecutor
from config import Config

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


class PositionMonitor:
    def __init__(self, client: TradingClient, config: Config, order_executor: OrderExecutor) -> None:
        self._client = client
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor
```

The `run` and `_check_positions` methods are unchanged.

- [ ] **Step 2: Run tests to confirm nothing is broken**

```
python -m pytest tests/test_position_monitor.py -v
```

Expected: all existing tests PASS (they test `compute_pnl_pct`, not the constructor).

- [ ] **Step 3: Commit**

```bash
git add trading/position_monitor.py
git commit -m "refactor: inject TradingClient into PositionMonitor"
```

---

## Task 4: Update NewsHandler to accept an injected client

**Files:**
- Modify: `news/news_handler.py`

- [ ] **Step 1: Update `NewsHandler.__init__`**

Replace `news/news_handler.py` in full:

```python
import asyncio
import logging
from alpaca.data.live import NewsDataStream
from alpaca.trading.client import TradingClient
from llm.llm_advisor import LLMAdvisor
from trading.order_executor import OrderExecutor
from config import Config

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(self, client: TradingClient, config: Config, llm_advisor: LLMAdvisor, order_executor: OrderExecutor) -> None:
        self._client = client
        self._config = config
        self._advisor = llm_advisor
        self._executor = order_executor

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

            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
            )

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            if decision.action == "buy" and decision.ticker:
                await self._executor.buy(decision.ticker)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    await self._executor.short(decision.ticker)
                else:
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                await self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")
```

- [ ] **Step 2: Verify imports are clean**

```
python -c "from news.news_handler import NewsHandler; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add news/news_handler.py
git commit -m "refactor: inject TradingClient into NewsHandler"
```

---

## Task 5: Update main.py to wire the shared client and seed open_dates

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Rewrite `main.py`**

Replace the entire file:

```python
import asyncio
import logging
from datetime import date, datetime
from rich.logging import RichHandler
import pytz
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import GetOrdersRequest
from alpaca.trading.enums import QueryOrderStatus, OrderStatus
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


def _make_trading_client(config: Config) -> TradingClient:
    return TradingClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
        paper=config.paper,
    )


def _load_open_positions(
    client: TradingClient,
) -> tuple[set[str], set[str], dict[str, date]]:
    positions = client.get_all_positions()
    held = {p.symbol for p in positions if p.side.value == "long"}
    shorted = {p.symbol for p in positions if p.side.value == "short"}
    if held:
        logger.info("Resuming with existing long positions: %s", held)
    if shorted:
        logger.info("Resuming with existing short positions: %s", shorted)

    # Seed _open_dates for positions opened today so the PDT guard
    # is not bypassed after a mid-day restart.
    et = pytz.timezone("America/New_York")
    today_start_et = et.localize(datetime.combine(date.today(), datetime.min.time()))
    orders = client.get_orders(
        GetOrdersRequest(status=QueryOrderStatus.CLOSED, after=today_start_et)
    )
    open_today = {
        o.symbol
        for o in orders
        if o.status == OrderStatus.FILLED and o.symbol in (held | shorted)
    }
    open_dates: dict[str, date] = {symbol: date.today() for symbol in open_today}
    if open_dates:
        logger.info("Seeding PDT open_dates from today's fills: %s", set(open_dates))

    return held, shorted, open_dates


async def main() -> None:
    config = load_config()
    client = _make_trading_client(config)
    held_tickers, shorted_tickers, open_dates = _load_open_positions(client)

    if config.telegram_enabled:
        notifier = TelegramNotifier(config.telegram_bot_token, config.telegram_chat_id)
    else:
        notifier = NoOpNotifier()

    order_executor = OrderExecutor(client, config, held_tickers, shorted_tickers, notifier, open_dates=open_dates)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(client, config, llm_advisor, order_executor)
    position_monitor = PositionMonitor(client, config, order_executor)

    coroutines = [news_handler.run(), position_monitor.run()]
    command_listener = None
    if config.telegram_enabled:
        command_listener = TelegramCommandListener(
            config.telegram_bot_token, config.telegram_chat_id, order_executor
        )
        coroutines.append(command_listener.run())

    logger.info("Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
                config.paper, config.trade_amount_usd,
                config.stop_loss_pct * 100, config.take_profit_pct * 100)

    try:
        await asyncio.gather(*coroutines)
    except asyncio.CancelledError:
        logger.info("Bot shutting down")
    finally:
        await notifier.aclose()
        if command_listener:
            await command_listener.aclose()


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Verify all imports are clean**

```
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run the full test suite**

```
python -m pytest tests/ -v
```

Expected: all tests PASS.

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: wire shared TradingClient and seed PDT open_dates from order history on startup"
```
