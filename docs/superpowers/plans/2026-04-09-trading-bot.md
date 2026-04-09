# Trading Bot Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a Python bot that reads real-time Alpaca news via WebSocket, uses Claude to decide buy/sell actions, and executes $5 notional trades on Alpaca with -5% stop-loss and +10% take-profit rules.

**Architecture:** Single async Python process using `asyncio.gather` to run a news WebSocket listener and a 30-second position monitor loop concurrently. Claude API evaluates each news event and returns a structured buy/sell/hold decision. A shared `held_tickers: set[str]` prevents duplicate buys and is initialized from live Alpaca positions at startup.

**Tech Stack:** Python 3.11+, `alpaca-py`, `anthropic`, `python-dotenv`, `pytest`

---

## File Map

| File | Responsibility |
|------|---------------|
| `requirements.txt` | Python dependencies |
| `.env.example` | Template for required environment variables |
| `config.py` | Load and validate `.env` into a typed `Config` dataclass |
| `order_executor.py` | Place and close Alpaca market orders; manage `held_tickers` set |
| `llm_advisor.py` | Call Claude API; parse response into a `Decision` dataclass |
| `position_monitor.py` | Poll open positions every 30s; trigger SL/TP sells |
| `news_handler.py` | Subscribe to Alpaca news WebSocket; route events to LLMAdvisor |
| `main.py` | Wire all components; run event loop |
| `README.md` | Setup and usage documentation |
| `tests/test_llm_advisor.py` | Tests for pure response-parsing logic |
| `tests/test_position_monitor.py` | Tests for pure P&L calculation logic |

---

### Task 1: Bootstrap project

**Files:**
- Create: `requirements.txt`
- Create: `.env.example`

- [ ] **Step 1: Create `requirements.txt`**

```
alpaca-py>=0.26.0
anthropic>=0.25.0
python-dotenv>=1.0.0
pytest>=8.0.0
```

- [ ] **Step 2: Create `.env.example`**

```
ALPACA_API_KEY=your_alpaca_api_key
ALPACA_SECRET_KEY=your_alpaca_secret_key
ALPACA_BASE_URL=https://paper-api.alpaca.markets
ANTHROPIC_API_KEY=your_anthropic_api_key
TRADE_AMOUNT_USD=5.0
STOP_LOSS_PCT=0.05
TAKE_PROFIT_PCT=0.10
```

- [ ] **Step 3: Install dependencies**

Run:
```bash
pip install -r requirements.txt
```
Expected: all packages install without errors.

- [ ] **Step 4: Copy `.env.example` to `.env` and fill in real paper-trading keys**

```bash
cp .env.example .env
```
Fill in `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`, and `ANTHROPIC_API_KEY` with real values.

- [ ] **Step 5: Commit**

```bash
git init
git add requirements.txt .env.example
git commit -m "feat: bootstrap project with dependencies"
```

---

### Task 2: Config loader

**Files:**
- Create: `config.py`

- [ ] **Step 1: Write `config.py`**

```python
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass
class Config:
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    anthropic_api_key: str
    trade_amount_usd: float
    stop_loss_pct: float
    take_profit_pct: float

    @property
    def paper(self) -> bool:
        return "paper" in self.alpaca_base_url


def load_config() -> Config:
    missing = [k for k in ("ALPACA_API_KEY", "ALPACA_SECRET_KEY", "ANTHROPIC_API_KEY") if not os.getenv(k)]
    if missing:
        raise ValueError(f"Missing required environment variables: {', '.join(missing)}")

    return Config(
        alpaca_api_key=os.environ["ALPACA_API_KEY"],
        alpaca_secret_key=os.environ["ALPACA_SECRET_KEY"],
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
        anthropic_api_key=os.environ["ANTHROPIC_API_KEY"],
        trade_amount_usd=float(os.getenv("TRADE_AMOUNT_USD", "5.0")),
        stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "0.05")),
        take_profit_pct=float(os.getenv("TAKE_PROFIT_PCT", "0.10")),
    )
```

- [ ] **Step 2: Verify config loads**

Run:
```bash
python -c "from config import load_config; c = load_config(); print(c)"
```
Expected: prints the `Config` dataclass with your values; no `ValueError`.

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add config loader from .env"
```

---

### Task 3: OrderExecutor

**Files:**
- Create: `order_executor.py`

- [ ] **Step 1: Write `order_executor.py`**

```python
import logging
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce
from config import Config

logger = logging.getLogger(__name__)


class OrderExecutor:
    def __init__(self, config: Config, held_tickers: set[str]) -> None:
        self._client = TradingClient(
            api_key=config.alpaca_api_key,
            secret_key=config.alpaca_secret_key,
            paper=config.paper,
        )
        self._trade_amount = config.trade_amount_usd
        self._held_tickers = held_tickers

    def buy(self, ticker: str) -> None:
        if ticker in self._held_tickers:
            logger.info("Skipping buy for %s — already held", ticker)
            return
        try:
            order = self._client.submit_order(
                MarketOrderRequest(
                    symbol=ticker,
                    notional=self._trade_amount,
                    side=OrderSide.BUY,
                    time_in_force=TimeInForce.DAY,
                )
            )
            self._held_tickers.add(ticker)
            logger.info("BUY %s $%.2f — order %s", ticker, self._trade_amount, order.id)
        except Exception as e:
            logger.error("Failed to buy %s: %s", ticker, e)

    def sell(self, ticker: str) -> None:
        try:
            self._client.close_position(ticker)
            self._held_tickers.discard(ticker)
            logger.info("SELL %s — position closed", ticker)
        except Exception as e:
            logger.error("Failed to sell %s: %s", ticker, e)
```

- [ ] **Step 2: Smoke-test with paper keys**

Run:
```bash
python -c "
from config import load_config
from order_executor import OrderExecutor
cfg = load_config()
ex = OrderExecutor(cfg, set())
print('OrderExecutor instantiated OK')
"
```
Expected: prints `OrderExecutor instantiated OK` without exceptions.

- [ ] **Step 3: Commit**

```bash
git add order_executor.py
git commit -m "feat: add OrderExecutor with buy/sell via alpaca-py"
```

---

### Task 4: LLMAdvisor

**Files:**
- Create: `llm_advisor.py`
- Create: `tests/__init__.py`
- Create: `tests/test_llm_advisor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/__init__.py` (empty), then `tests/test_llm_advisor.py`:

```python
import pytest
from llm_advisor import Decision, _parse_response


def test_parse_buy_decision():
    text = '{"action": "buy", "ticker": "AAPL", "reasoning": "Strong earnings beat"}'
    decision = _parse_response(text)
    assert decision.action == "buy"
    assert decision.ticker == "AAPL"
    assert "earnings" in decision.reasoning


def test_parse_sell_decision():
    text = '{"action": "sell", "ticker": "TSLA", "reasoning": "Product recall announced"}'
    decision = _parse_response(text)
    assert decision.action == "sell"
    assert decision.ticker == "TSLA"


def test_parse_hold_decision():
    text = '{"action": "hold", "ticker": null, "reasoning": "Neutral news"}'
    decision = _parse_response(text)
    assert decision.action == "hold"
    assert decision.ticker is None


def test_parse_response_with_surrounding_text():
    text = 'Sure! Here is my answer:\n{"action": "buy", "ticker": "NVDA", "reasoning": "AI demand"}\nHope that helps.'
    decision = _parse_response(text)
    assert decision.action == "buy"
    assert decision.ticker == "NVDA"


def test_parse_invalid_response_raises():
    with pytest.raises(ValueError):
        _parse_response("I cannot determine what to do here.")
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_llm_advisor.py -v
```
Expected: `ImportError` — `llm_advisor` does not exist yet.

- [ ] **Step 3: Write `llm_advisor.py`**

```python
import json
import logging
import re
from dataclasses import dataclass
from typing import Literal

import anthropic
from config import Config

logger = logging.getLogger(__name__)

_MODEL = "claude-opus-4-6"

_PROMPT_TEMPLATE = """\
You are a stock trading assistant. Based on the news below, decide whether to buy, sell, or hold.

News:
Headline: {headline}
Summary: {summary}
Tickers mentioned: {symbols}

Currently held positions: {held_tickers}

Rules:
- Only recommend buying a ticker directly mentioned in the news.
- Only recommend selling if the news is clearly negative for a ticker you currently hold.
- Be conservative — only act on clearly bullish or clearly bearish news.
- Return ONLY a JSON object, nothing else:
  {{"action": "buy" | "sell" | "hold", "ticker": "SYMBOL or null", "reasoning": "one sentence"}}
"""


@dataclass
class Decision:
    action: Literal["buy", "sell", "hold"]
    ticker: str | None
    reasoning: str


def _parse_response(text: str) -> Decision:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise ValueError(f"No JSON object found in response: {text!r}")
    data = json.loads(match.group())
    return Decision(
        action=data["action"],
        ticker=data.get("ticker") or None,
        reasoning=data.get("reasoning", ""),
    )


class LLMAdvisor:
    def __init__(self, config: Config) -> None:
        self._client = anthropic.Anthropic(api_key=config.anthropic_api_key)

    def analyze(self, headline: str, summary: str, symbols: list[str], held_tickers: set[str]) -> Decision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
        )
        try:
            message = self._client.messages.create(
                model=_MODEL,
                max_tokens=256,
                messages=[{"role": "user", "content": prompt}],
            )
            text = message.content[0].text
            return _parse_response(text)
        except Exception as e:
            logger.error("LLM error: %s", e)
            return Decision(action="hold", ticker=None, reasoning=f"error: {e}")
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_llm_advisor.py -v
```
Expected:
```
tests/test_llm_advisor.py::test_parse_buy_decision PASSED
tests/test_llm_advisor.py::test_parse_sell_decision PASSED
tests/test_llm_advisor.py::test_parse_hold_decision PASSED
tests/test_llm_advisor.py::test_parse_response_with_surrounding_text PASSED
tests/test_llm_advisor.py::test_parse_invalid_response_raises PASSED
5 passed
```

- [ ] **Step 5: Commit**

```bash
git add llm_advisor.py tests/__init__.py tests/test_llm_advisor.py
git commit -m "feat: add LLMAdvisor with Claude API integration and parse tests"
```

---

### Task 5: PositionMonitor

**Files:**
- Create: `position_monitor.py`
- Create: `tests/test_position_monitor.py`

- [ ] **Step 1: Write the failing test**

Create `tests/test_position_monitor.py`:

```python
import pytest
from position_monitor import compute_pnl_pct


def test_pnl_at_stop_loss_boundary():
    # exactly -5% → should trigger stop-loss
    assert compute_pnl_pct(100.0, 95.0) == pytest.approx(-0.05)


def test_pnl_below_stop_loss():
    assert compute_pnl_pct(100.0, 90.0) == pytest.approx(-0.10)


def test_pnl_at_take_profit_boundary():
    # exactly +10% → should trigger take-profit
    assert compute_pnl_pct(100.0, 110.0) == pytest.approx(0.10)


def test_pnl_above_take_profit():
    assert compute_pnl_pct(100.0, 115.0) == pytest.approx(0.15)


def test_pnl_flat():
    assert compute_pnl_pct(50.0, 50.0) == pytest.approx(0.0)
```

- [ ] **Step 2: Run tests to confirm they fail**

Run:
```bash
pytest tests/test_position_monitor.py -v
```
Expected: `ImportError` — `position_monitor` does not exist yet.

- [ ] **Step 3: Write `position_monitor.py`**

```python
import asyncio
import logging
from order_executor import OrderExecutor
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
            except Exception as e:
                logger.error("Position monitor poll failed: %s", e)

    def _check_positions(self) -> None:
        positions = self._client.get_all_positions()
        for pos in positions:
            ticker = pos.symbol
            entry = float(pos.avg_entry_price)
            current = float(pos.current_price)
            pnl = compute_pnl_pct(entry, current)

            if pnl <= -self._stop_loss:
                logger.info("Stop-loss triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                self._executor.sell(ticker)
            elif pnl >= self._take_profit:
                logger.info("Take-profit triggered for %s (P&L %.2f%%)", ticker, pnl * 100)
                self._executor.sell(ticker)
```

- [ ] **Step 4: Run tests to confirm they pass**

Run:
```bash
pytest tests/test_position_monitor.py -v
```
Expected:
```
tests/test_position_monitor.py::test_pnl_at_stop_loss_boundary PASSED
tests/test_position_monitor.py::test_pnl_below_stop_loss PASSED
tests/test_position_monitor.py::test_pnl_at_take_profit_boundary PASSED
tests/test_position_monitor.py::test_pnl_above_take_profit PASSED
tests/test_position_monitor.py::test_pnl_flat PASSED
5 passed
```

- [ ] **Step 5: Commit**

```bash
git add position_monitor.py tests/test_position_monitor.py
git commit -m "feat: add PositionMonitor with SL/TP logic and tests"
```

---

### Task 6: NewsHandler

**Files:**
- Create: `news_handler.py`

- [ ] **Step 1: Write `news_handler.py`**

```python
import logging
from alpaca.data.live import NewsDataStream
from llm_advisor import LLMAdvisor
from order_executor import OrderExecutor
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
                await stream._run_forever()
            except Exception as e:
                logger.error("News stream error: %s — reconnecting in 5s", e)
                import asyncio
                await asyncio.sleep(5)

    async def _handle_news(self, news) -> None:
        headline = getattr(news, "headline", "")
        summary = getattr(news, "summary", "")
        symbols: list[str] = getattr(news, "symbols", [])

        logger.info("News received: %s | tickers: %s", headline, symbols)

        if not symbols:
            logger.debug("No tickers in news event — skipping")
            return

        decision = self._advisor.analyze(
            headline=headline,
            summary=summary,
            symbols=symbols,
            held_tickers=self._executor._held_tickers,
        )

        logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

        if decision.action == "buy" and decision.ticker:
            self._executor.buy(decision.ticker)
        elif decision.action == "sell" and decision.ticker:
            self._executor.sell(decision.ticker)
```

- [ ] **Step 2: Commit**

```bash
git add news_handler.py
git commit -m "feat: add NewsHandler subscribing to Alpaca news WebSocket"
```

---

### Task 7: main.py

**Files:**
- Create: `main.py`

- [ ] **Step 1: Write `main.py`**

```python
import asyncio
import logging
from config import load_config
from order_executor import OrderExecutor
from llm_advisor import LLMAdvisor
from news_handler import NewsHandler
from position_monitor import PositionMonitor
from alpaca.trading.client import TradingClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)


def _load_held_tickers(config) -> set[str]:
    client = TradingClient(
        api_key=config.alpaca_api_key,
        secret_key=config.alpaca_secret_key,
        paper=config.paper,
    )
    positions = client.get_all_positions()
    tickers = {p.symbol for p in positions}
    if tickers:
        logger.info("Resuming with existing positions: %s", tickers)
    return tickers


async def main() -> None:
    config = load_config()
    held_tickers = _load_held_tickers(config)

    order_executor = OrderExecutor(config, held_tickers)
    llm_advisor = LLMAdvisor(config)
    news_handler = NewsHandler(config, llm_advisor, order_executor)
    position_monitor = PositionMonitor(config, order_executor)

    logger.info("Bot starting — paper=%s, trade_amount=$%.2f, SL=%.0f%%, TP=%.0f%%",
                config.paper, config.trade_amount_usd,
                config.stop_loss_pct * 100, config.take_profit_pct * 100)

    await asyncio.gather(
        news_handler.run(),
        position_monitor.run(),
    )


if __name__ == "__main__":
    asyncio.run(main())
```

- [ ] **Step 2: Run the bot against paper trading**

Run:
```bash
python main.py
```
Expected output (on startup):
```
2026-04-09 ... INFO __main__ — Bot starting — paper=True, trade_amount=$5.00, SL=5%, TP=10%
2026-04-09 ... INFO news_handler — News WebSocket connected — listening for news
```
The bot will log each news event and LLM decision as they arrive. Verify in the [Alpaca paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) that orders appear when Claude returns a buy/sell decision.

- [ ] **Step 3: Commit**

```bash
git add main.py
git commit -m "feat: add main entry point wiring all components via asyncio.gather"
```

---

### Task 8: README.md

**Files:**
- Create: `README.md`

- [ ] **Step 1: Write `README.md`**

```markdown
# Alpaca News Trading Bot

Listens to real-time news from Alpaca's WebSocket feed, uses Claude (Anthropic) to decide whether to buy or sell a stock based on the news, and executes trades on Alpaca.

## How it works

1. Connects to Alpaca's news WebSocket and receives live news events.
2. Sends each news headline, summary, and mentioned tickers to Claude.
3. Claude returns a `buy`, `sell`, or `hold` decision.
4. On `buy`: places a $5 notional market order for the ticker.
5. On `sell`: closes the full position for the ticker.
6. Every 30 seconds, checks all open positions and automatically sells if:
   - P&L drops to **-5%** (stop-loss)
   - P&L reaches **+10%** (take-profit)

## Prerequisites

- Python 3.11+
- An [Alpaca](https://alpaca.markets) account (paper trading is free)
- An [Anthropic](https://console.anthropic.com) account with API access

## Setup

### 1. Get Alpaca paper trading API keys

1. Sign up or log in at [alpaca.markets](https://alpaca.markets)
2. Switch to **Paper Trading** mode in the top-left dropdown
3. Go to **Overview → API Keys** → generate a new key pair
4. Copy the **API Key ID** and **Secret Key**

### 2. Get a Claude API key

1. Go to [console.anthropic.com](https://console.anthropic.com)
2. Navigate to **API Keys** → **Create Key**
3. Copy the key

### 3. Configure environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your values:

| Variable | Description | Default |
|----------|-------------|---------|
| `ALPACA_API_KEY` | Alpaca API key ID | required |
| `ALPACA_SECRET_KEY` | Alpaca secret key | required |
| `ALPACA_BASE_URL` | Alpaca base URL | `https://paper-api.alpaca.markets` |
| `ANTHROPIC_API_KEY` | Anthropic API key | required |
| `TRADE_AMOUNT_USD` | Dollar amount per buy order | `5.0` |
| `STOP_LOSS_PCT` | Stop-loss threshold (decimal) | `0.05` (5%) |
| `TAKE_PROFIT_PCT` | Take-profit threshold (decimal) | `0.10` (10%) |

### 4. Install dependencies

```bash
pip install -r requirements.txt
```

### 5. Run the bot

```bash
python main.py
```

The bot runs until stopped (`Ctrl+C`). All decisions and orders are logged to stdout.

## Testing

All testing is done against Alpaca's paper trading environment. After starting the bot, monitor the [paper trading dashboard](https://app.alpaca.markets/paper/dashboard/overview) to see orders placed in response to news.

To run the unit tests for pure logic (no external calls):

```bash
pytest tests/ -v
```

## Limitations

- The bot runs only while the process is active — there is no scheduling or market-hours gating.
- Only stocks with tickers mentioned in the news are eligible for trades.
- One position per ticker at a time (duplicate buy signals are skipped).
- For live trading, change `ALPACA_BASE_URL` to `https://api.alpaca.markets` and use live API keys.
```

- [ ] **Step 2: Commit**

```bash
git add README.md
git commit -m "docs: add README with setup and usage instructions"
```

---

### Task 9: Run all tests

- [ ] **Step 1: Run the full test suite**

Run:
```bash
pytest tests/ -v
```
Expected:
```
tests/test_llm_advisor.py::test_parse_buy_decision PASSED
tests/test_llm_advisor.py::test_parse_sell_decision PASSED
tests/test_llm_advisor.py::test_parse_hold_decision PASSED
tests/test_llm_advisor.py::test_parse_response_with_surrounding_text PASSED
tests/test_llm_advisor.py::test_parse_invalid_response_raises PASSED
tests/test_position_monitor.py::test_pnl_at_stop_loss_boundary PASSED
tests/test_position_monitor.py::test_pnl_below_stop_loss PASSED
tests/test_position_monitor.py::test_pnl_at_take_profit_boundary PASSED
tests/test_position_monitor.py::test_pnl_above_take_profit PASSED
tests/test_position_monitor.py::test_pnl_flat PASSED
10 passed
```
