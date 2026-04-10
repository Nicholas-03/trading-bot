# Folder Structure Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Reorganize flat root-level Python files into `trading/`, `llm/`, and `news/` feature folders without changing any logic.

**Architecture:** Each domain folder gets an `__init__.py` and its files. All cross-module imports are updated to use the folder prefix (e.g. `from trading.order_executor import OrderExecutor`). `config.py` and `main.py` stay at root.

**Tech Stack:** Python 3.11+, pytest

---

### Task 1: Create folder scaffolding

**Files:**
- Create: `trading/__init__.py`
- Create: `llm/__init__.py`
- Create: `news/__init__.py`

- [ ] **Step 1: Create the three empty `__init__.py` files**

```bash
# Run from project root
touch trading/__init__.py llm/__init__.py news/__init__.py
```

Or create each file manually with empty content (0 bytes).

- [ ] **Step 2: Verify folders exist**

```bash
ls trading/ llm/ news/
```

Expected output: each directory contains `__init__.py`.

---

### Task 2: Move and update `trading/order_executor.py`

**Files:**
- Create: `trading/order_executor.py` (moved from `order_executor.py`)
- Delete: `order_executor.py`

`order_executor.py` only imports `config` (stays at root) — no import changes needed.

- [ ] **Step 1: Create `trading/order_executor.py` with this exact content**

```python
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from alpaca.common.exceptions import APIError
from config import Config

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, config: Config, held_tickers: set[str], shorted_tickers: set[str]) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._notional_usd = config.trade_amount_usd
        self._short_qty = config.short_qty
        self._held_tickers = held_tickers
        self._shorted_tickers = shorted_tickers

    @property
    def held_tickers(self) -> frozenset[str]:
        return frozenset(self._held_tickers)

    @property
    def shorted_tickers(self) -> frozenset[str]:
        return frozenset(self._shorted_tickers)

    def buy(self, ticker: str) -> None:
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
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)

    def short(self, ticker: str) -> None:
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
        except Exception as e:
            logger.error("Failed to short %s: %s", ticker, e)

    def sell(self, ticker: str) -> None:
        """Close a position — works for both long (sell) and short (cover)."""
        if ticker not in self._held_tickers and ticker not in self._shorted_tickers:
            logger.warning("Sell/cover called for %s but no open position — skipping", ticker)
            return
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            self._shorted_tickers.discard(ticker)
            logger.info("CLOSED position for %s", ticker)
        except APIError as e:
            status = getattr(e, "status_code", None)
            if status in (404, 422):
                self._held_tickers.discard(ticker)
                self._shorted_tickers.discard(ticker)
                logger.warning("Close %s — position not found (status %s), removing from tracking", ticker, status)
            else:
                logger.error("Failed to close position for %s: %s", ticker, e)
        except Exception as e:
            logger.error("Failed to close position for %s: %s", ticker, e)
```

- [ ] **Step 2: Delete the old root-level file**

```bash
rm order_executor.py
```

---

### Task 3: Move and update `trading/position_monitor.py`

**Files:**
- Create: `trading/position_monitor.py` (moved from `position_monitor.py`)
- Delete: `position_monitor.py`

Import change: `from order_executor import OrderExecutor` → `from trading.order_executor import OrderExecutor`

- [ ] **Step 1: Create `trading/position_monitor.py` with this exact content**

```python
import asyncio
import logging
from trading.order_executor import OrderExecutor
from config import Config
from alpaca.trading.client import TradingClient

logger = logging.getLogger(__name__)


def compute_pnl_pct(avg_entry_price: float, current_price: float) -> float:
    return (current_price - avg_entry_price) / avg_entry_price


class PositionMonitor:
    def __init__(self, config: Config, order_executor: OrderExecutor) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._stop_loss = config.stop_loss_pct
        self._take_profit = config.take_profit_pct
        self._executor = order_executor

    async def run(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                self._check_positions()
            except Exception:
                logger.exception("Position monitor poll failed")

    def _check_positions(self) -> None:
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
                    self._executor.sell(ticker)
                elif pnl >= self._take_profit:
                    logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                    self._executor.sell(ticker)
            except Exception:
                logger.exception("Error processing position %s", pos.symbol)
```

- [ ] **Step 2: Delete the old root-level file**

```bash
rm position_monitor.py
```

---

### Task 4: Move and update `llm/llm_advisor.py`

**Files:**
- Create: `llm/llm_advisor.py` (moved from `llm_advisor.py`)
- Delete: `llm_advisor.py`

`llm_advisor.py` only imports `config` (stays at root) — no import changes needed.

- [ ] **Step 1: Create `llm/llm_advisor.py` with this exact content**

```python
import asyncio
import json
import logging
from dataclasses import dataclass
from typing import Literal

import anthropic
from google import genai
from google.genai import errors as genai_errors
from config import Config

logger = logging.getLogger(__name__)

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide the best action.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}

Currently held long positions: {held_tickers}
Currently held short positions: {shorted_tickers}

Actions available:
- buy: open a long position (bet the price goes UP). Only for tickers in the news.
- short: open a short position (bet the price goes DOWN). Only for tickers in the news.
- sell: close an open long OR short position. Only for tickers you currently hold.
- hold: do nothing.

Rules:
- Only act on tickers directly mentioned in the news.
- Be conservative — only act on clearly bullish or clearly bearish news.
- Do not open a long and short on the same ticker simultaneously.
- Return ONLY a valid JSON object, nothing else. Use exactly one of these formats:
  {{"action": "buy", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "short", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "sell", "ticker": "SYMBOL", "reasoning": "one sentence"}}
  {{"action": "hold", "ticker": null, "reasoning": "one sentence"}}
"""

_VALID_ACTIONS = frozenset({"buy", "short", "sell", "hold"})


@dataclass
class Decision:
    action: Literal["buy", "short", "sell", "hold"]
    ticker: str | None
    reasoning: str


def _parse_response(text: str) -> Decision:
    decoder = json.JSONDecoder()
    idx = 0
    last_action_error: ValueError | None = None
    while idx < len(text):
        pos = text.find("{", idx)
        if pos == -1:
            break
        try:
            data, _ = decoder.raw_decode(text, pos)
            action = data.get("action", "")
            if action not in _VALID_ACTIONS:
                last_action_error = ValueError(f"Unexpected action {action!r}; expected one of {_VALID_ACTIONS}")
                idx = pos + 1
                continue
            raw_ticker = data.get("ticker")
            ticker = None if raw_ticker in (None, "null", "") else str(raw_ticker)
            return Decision(
                action=action,
                ticker=ticker,
                reasoning=data.get("reasoning", ""),
            )
        except (json.JSONDecodeError, KeyError):
            idx = pos + 1
    if last_action_error is not None:
        raise last_action_error
    raise ValueError(f"No valid decision JSON found in response: {text!r}")


class LLMAdvisor:
    def __init__(self, config: Config) -> None:
        self._provider = config.llm_provider
        if self._provider == "claude":
            self._claude = anthropic.Anthropic(api_key=config.anthropic_api_key)
            self._claude_model = config.anthropic_model
        else:
            self._gemini = genai.Client(api_key=config.google_api_key)
            self._gemini_model = config.gemini_model

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
    ) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
            shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
        )
        try:
            if self._provider == "claude":
                text = await self._call_claude(prompt)
            else:
                text = await self._call_gemini(prompt)
            return _parse_response(text)
        except ValueError as e:
            logger.error("LLM parse error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"parse error: {e}")
        except Exception as e:
            logger.error("LLM API error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"api error: {e}")

    async def _call_claude(self, prompt: str) -> str:
        # Run the blocking Anthropic SDK call in a thread so it doesn't freeze the event loop
        message = await asyncio.to_thread(
            self._claude.messages.create,
            model=self._claude_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text

    async def _call_gemini(self, prompt: str) -> str:
        max_retries = 3
        for attempt in range(max_retries + 1):
            try:
                response = await self._gemini.aio.models.generate_content(
                    model=self._gemini_model,
                    contents=prompt,
                )
                return response.text
            except genai_errors.ServerError as e:
                if attempt < max_retries:
                    wait = 2 ** attempt  # 1s, 2s, 4s
                    logger.warning(
                        "Gemini 503 (attempt %d/%d), retrying in %ds: %s",
                        attempt + 1, max_retries, wait, e,
                    )
                    await asyncio.sleep(wait)
                else:
                    raise
```

- [ ] **Step 2: Delete the old root-level file**

```bash
rm llm_advisor.py
```

---

### Task 5: Move and update `news/news_handler.py`

**Files:**
- Create: `news/news_handler.py` (moved from `news_handler.py`)
- Delete: `news_handler.py`

Import changes:
- `from llm_advisor import LLMAdvisor` → `from llm.llm_advisor import LLMAdvisor`
- `from order_executor import OrderExecutor` → `from trading.order_executor import OrderExecutor`

- [ ] **Step 1: Create `news/news_handler.py` with this exact content**

```python
import asyncio
import logging
from alpaca.data.live import NewsDataStream
from llm.llm_advisor import LLMAdvisor
from trading.order_executor import OrderExecutor
from config import Config

logger = logging.getLogger(__name__)


class NewsHandler:
    def __init__(self, config: Config, llm_advisor: LLMAdvisor, order_executor: OrderExecutor) -> None:
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
                self._executor.buy(decision.ticker)
            elif decision.action == "short" and decision.ticker:
                if self._config.allow_short:
                    self._executor.short(decision.ticker)
                else:
                    logger.info("Short selling disabled — skipping short for %s", decision.ticker)
            elif decision.action == "sell" and decision.ticker:
                self._executor.sell(decision.ticker)
        except Exception:
            logger.exception("Unhandled error processing news event")
```

- [ ] **Step 2: Delete the old root-level file**

```bash
rm news_handler.py
```

---

### Task 6: Update `main.py` imports

**Files:**
- Modify: `main.py`

Import changes:
- `from order_executor import OrderExecutor` → `from trading.order_executor import OrderExecutor`
- `from llm_advisor import LLMAdvisor` → `from llm.llm_advisor import LLMAdvisor`
- `from news_handler import NewsHandler` → `from news.news_handler import NewsHandler`
- `from position_monitor import PositionMonitor` → `from trading.position_monitor import PositionMonitor`

- [ ] **Step 1: Replace the import block in `main.py`**

Replace lines 5–8:
```python
from order_executor import OrderExecutor
from llm_advisor import LLMAdvisor
from news_handler import NewsHandler
from position_monitor import PositionMonitor
```

With:
```python
from trading.order_executor import OrderExecutor
from llm.llm_advisor import LLMAdvisor
from news.news_handler import NewsHandler
from trading.position_monitor import PositionMonitor
```

- [ ] **Step 2: Verify `main.py` imports cleanly**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

---

### Task 7: Update test imports

**Files:**
- Modify: `tests/test_llm_advisor.py`
- Modify: `tests/test_position_monitor.py`

- [ ] **Step 1: Update `tests/test_llm_advisor.py` import**

Replace:
```python
from llm_advisor import Decision, _parse_response
```

With:
```python
from llm.llm_advisor import Decision, _parse_response
```

- [ ] **Step 2: Update `tests/test_position_monitor.py` import**

Replace:
```python
from position_monitor import compute_pnl_pct
```

With:
```python
from trading.position_monitor import compute_pnl_pct
```

- [ ] **Step 3: Run all tests**

```bash
python -m pytest tests/ -v
```

Expected: 13 tests pass, 0 failures.

---

### Task 8: Update CLAUDE.md and commit

**Files:**
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update the Key Files table in `CLAUDE.md`**

Replace the existing Key Files table:

```markdown
| File | Responsibility |
|------|---------------|
| `config.py` | Load/validate `.env` into frozen `Config` dataclass |
| `trading/order_executor.py` | Buy/sell via alpaca-py; manage `held_tickers` |
| `llm/llm_advisor.py` | Call Claude API; parse `Decision(action, ticker, reasoning)` |
| `trading/position_monitor.py` | SL/TP loop; `compute_pnl_pct()` is pure and tested |
| `news/news_handler.py` | WebSocket subscriber; routes LLM decisions to executor |
| `main.py` | Entry point; wires all components |
```

- [ ] **Step 2: Update import references in the Architecture section of `CLAUDE.md`**

The architecture section references `NewsHandler` accessing `OrderExecutor.held_tickers` — no path changes needed there, just ensure any file paths mentioned match the new locations.

- [ ] **Step 3: Run tests one final time**

```bash
python -m pytest tests/ -v
```

Expected: 13 tests pass, 0 failures.

- [ ] **Step 4: Commit all changes**

```bash
git add trading/ llm/ news/ main.py tests/test_llm_advisor.py tests/test_position_monitor.py CLAUDE.md
git commit -m "refactor: reorganize source files into trading/, llm/, news/ folders"
```
