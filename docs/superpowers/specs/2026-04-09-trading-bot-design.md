# Trading Bot Design

**Date:** 2026-04-09  
**Status:** Approved

## Overview

A Python-based automated trading bot that listens to real-time news from Alpaca's WebSocket feed, uses the Claude LLM to decide whether to buy or sell a stock based on the news, and executes trades on Alpaca. It runs as a single async process until stopped.

## Decisions

| Parameter | Value |
|-----------|-------|
| Language | Python |
| Broker | Alpaca (paper + live) |
| LLM | Claude API (Anthropic) |
| Trade size | $5 notional per buy |
| Stop-loss | -5% from entry |
| Take-profit | +10% from entry |
| Position monitor interval | 30 seconds |

## Architecture & Data Flow

```
Alpaca News WebSocket
        |
        v
  NewsHandler (async)
        |
   [news event]
        |
        v
  LLMAdvisor (Claude API)
   - receives headline + body + tickers mentioned
   - returns: action (buy/hold/sell), ticker, reasoning
        |
   [buy signal]          [sell signal]
        |                      |
        v                      v
  OrderExecutor         OrderExecutor
  - market buy $5       - market sell full position
        |
        v
  PositionMonitor (async loop, every 30s)
  - fetches open positions from Alpaca
  - if P&L <= -5% → market sell
  - if P&L >= +10% → market sell
```

Config (API keys, thresholds) lives in `.env`. No database — Alpaca's API is the source of truth for positions and orders.

## Components

### `main.py`
Entry point. Loads config, wires all modules together, starts the Alpaca news WebSocket connection and the position monitor loop concurrently via `asyncio.gather`.

### `news_handler.py`
Alpaca WebSocket callback. Receives raw news events, extracts `headline`, `summary`, and `symbols` (tickers mentioned), passes them to `LLMAdvisor`. If Claude returns a buy signal, calls `OrderExecutor.buy()`. If it returns a sell signal for a held ticker, calls `OrderExecutor.sell()`.

### `llm_advisor.py`
Wraps the Claude API. Builds a structured prompt containing the news headline, summary, tickers mentioned, and the list of currently held tickers. Parses the response into a typed decision:
```python
@dataclass
class Decision:
    action: Literal["buy", "sell", "hold"]
    ticker: str | None
    reasoning: str
```
If the API call fails or the response cannot be parsed, logs the error and returns `Decision(action="hold", ...)`.

### `order_executor.py`
Wraps `alpaca-py`. Exposes two methods:
- `buy(ticker: str, dollars: float)` — places a notional market order
- `sell(ticker: str)` — places a market sell for the full position quantity

On order failure, logs the full Alpaca error and does not retry.

### `position_monitor.py`
Async loop running every 30 seconds. Fetches all open positions from Alpaca, computes P&L percentage as `(current_price - avg_entry_price) / avg_entry_price`, and calls `OrderExecutor.sell()` when SL or TP is triggered. A single failed poll cycle is logged and skipped — the loop never crashes.

### `config.py`
Loads and validates all values from `.env`:
- `ALPACA_API_KEY`
- `ALPACA_SECRET_KEY`
- `ALPACA_BASE_URL` (paper vs live)
- `ANTHROPIC_API_KEY`
- `TRADE_AMOUNT_USD` (default: 5.0)
- `STOP_LOSS_PCT` (default: 0.05)
- `TAKE_PROFIT_PCT` (default: 0.10)

## Error Handling

| Scenario | Behavior |
|----------|----------|
| WebSocket disconnect | `alpaca-py` auto-reconnects; on exhausted retries, log and exit (let process manager restart) |
| Claude API error or bad response | Log, skip news event, return `hold` |
| Order placement failure | Log full Alpaca error, do not retry |
| Position monitor poll failure | Log, continue loop |
| Buy signal for already-held ticker | Skip — tracked via in-memory set of held tickers |

## Testing

All testing is done against **Alpaca's paper trading environment** using paper API keys. No mocks. Validate end-to-end by observing paper orders appear in the Alpaca dashboard when news flows in.

## Documentation

A `README.md` at the project root will cover:
- Prerequisites (Python version, Alpaca account, Anthropic account)
- How to obtain Alpaca paper trading API keys
- How to obtain a Claude API key
- `.env` setup and all available configuration values
- How to install dependencies and run the bot
- What the bot does and its known limitations (e.g. market hours, no scheduling)
