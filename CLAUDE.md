# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Run tests
python -m pytest tests/ -v

# Run the bot (requires .env with real API keys)
python main.py

# Verify all modules import cleanly
python -c "import main; print('OK')"
```

## Architecture

Single async Python process. Two coroutines run concurrently via `asyncio.gather` in `main.py`:

1. **`NewsHandler.run()`** — subscribes to Alpaca's news WebSocket (`NewsDataStream`, all symbols `"*"`). For each news event, calls `LLMAdvisor.analyze()` (non-blocking via `asyncio.to_thread`) and routes `buy`/`sell` decisions to `OrderExecutor`. Uses `TradierClient.get_clock()` for market-hours gating.

2. **`PositionMonitor.run()`** — polls open Tradier positions every 30 seconds. Sells any position where P&L ≤ stop-loss or ≥ take-profit threshold. Also fires EOD/weekly P&L reports at 16:00 ET.

**Shared state:** A `held_tickers: set[str]` is created in `main.py`, seeded from live Tradier positions at startup, and passed to `OrderExecutor`. `NewsHandler` accesses it via the public `OrderExecutor.held_tickers` property (returns `frozenset`). This prevents duplicate buys for the same ticker.

**Config:** All settings live in `.env` (copy from `.env.example`). Loaded via `config.py`'s `load_config()` which validates required keys and numeric ranges. `Config` is a frozen dataclass. Alpaca credentials (`ALPACA_API_KEY`/`ALPACA_SECRET_KEY`) are still required — used only for the `NewsDataStream` feed, not for trading.

**LLM:** `llm_advisor.py` calls Claude (`claude-opus-4-6`) with a structured prompt and parses the JSON response using an incremental `json.raw_decode` scan (not a greedy regex). Valid actions: `buy`, `sell`, `hold`. On any error, returns a safe `hold`.

**Orders:** `order_executor.py` places DAY market orders via Tradier's REST API (`TradierClient`). Fetches a live quote before each buy to convert notional USD → share quantity. Tracks in-memory P&L (daily/weekly) for EOD reports. All `TradierClient` calls are wrapped in `asyncio.to_thread` to avoid blocking the event loop.

## Key Files

| File | Responsibility |
|------|---------------|
| `config.py` | Load/validate `.env` into frozen `Config` dataclass |
| `trading/tradier_client.py` | httpx wrapper for Tradier REST API; pure parsing helpers |
| `trading/order_executor.py` | Buy/sell/short via Tradier; manage `held_tickers`; in-memory P&L tracking |
| `llm/llm_advisor.py` | Call Claude API; parse `Decision(action, ticker, reasoning)` |
| `trading/position_monitor.py` | SL/TP loop; `compute_pnl_pct()` is pure and tested |
| `news/news_handler.py` | WebSocket subscriber; routes LLM decisions to executor |
| `main.py` | Entry point; wires all components |

## Testing

Tests cover pure logic and in-memory state (no live API calls):
- `tests/test_llm_advisor.py` — 8 tests for `_parse_response()`
- `tests/test_position_monitor.py` — 13 tests for `compute_pnl_pct()` and `_should_fire_report()`
- `tests/test_tradier_client.py` — 10 tests for `_parse_positions()` and `_parse_quotes()`
- `tests/test_order_executor.py` — 14 tests for `_monday_of()`, daily/weekly summary resets, P&L accumulation

End-to-end testing is done against Tradier's sandbox environment. Set `TRADIER_PAPER=true` and use sandbox credentials.

## Notes

- `NewsDataStream._run_forever()` is called directly (private method) because `stream.run()` calls `asyncio.run()` internally, which conflicts with our existing event loop. This is a known alpaca-py constraint — see the comment in `news_handler.py`.
- `alpaca-py` is pinned to `>=0.38.0,<1.0.0` because `_run_forever` is private and could change in a major version.
