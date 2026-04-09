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

1. **`NewsHandler.run()`** — subscribes to Alpaca's news WebSocket (`NewsDataStream`, all symbols `"*"`). For each news event, calls `LLMAdvisor.analyze()` (non-blocking via `asyncio.to_thread`) and routes `buy`/`sell` decisions to `OrderExecutor`.

2. **`PositionMonitor.run()`** — polls open Alpaca positions every 30 seconds. Sells any position where P&L ≤ -5% (stop-loss) or ≥ +10% (take-profit).

**Shared state:** A `held_tickers: set[str]` is created in `main.py`, seeded from live Alpaca positions at startup, and passed to `OrderExecutor`. `NewsHandler` accesses it via the public `OrderExecutor.held_tickers` property (returns `frozenset`). This prevents duplicate buys for the same ticker.

**Config:** All settings live in `.env` (copy from `.env.example`). Loaded via `config.py`'s `load_config()` which validates required keys and numeric ranges. `Config` is a frozen dataclass.

**LLM:** `llm_advisor.py` calls Claude (`claude-opus-4-6`) with a structured prompt and parses the JSON response using an incremental `json.raw_decode` scan (not a greedy regex). Valid actions: `buy`, `sell`, `hold`. On any error, returns a safe `hold`.

**Orders:** `order_executor.py` places `$5` notional DAY market orders via `alpaca-py`. Handles `APIError` 404/422 on sell (position already gone). The blocking Anthropic SDK call is wrapped in `asyncio.to_thread` to avoid freezing the event loop during news bursts.

## Key Files

| File | Responsibility |
|------|---------------|
| `config.py` | Load/validate `.env` into frozen `Config` dataclass |
| `order_executor.py` | Buy/sell via alpaca-py; manage `held_tickers` |
| `llm_advisor.py` | Call Claude API; parse `Decision(action, ticker, reasoning)` |
| `position_monitor.py` | SL/TP loop; `compute_pnl_pct()` is pure and tested |
| `news_handler.py` | WebSocket subscriber; routes LLM decisions to executor |
| `main.py` | Entry point; wires all components |

## Testing

Tests cover only pure logic (no mocks, no external calls):
- `tests/test_llm_advisor.py` — 8 tests for `_parse_response()`
- `tests/test_position_monitor.py` — 5 tests for `compute_pnl_pct()`

End-to-end testing is done against Alpaca's paper trading environment. Use paper API keys (`ALPACA_BASE_URL=https://paper-api.alpaca.markets`).

## Notes

- `NewsDataStream._run_forever()` is called directly (private method) because `stream.run()` calls `asyncio.run()` internally, which conflicts with our existing event loop. This is a known alpaca-py constraint — see the comment in `news_handler.py`.
- `alpaca-py` is pinned to `>=0.38.0,<1.0.0` because `_run_forever` is private and could change in a major version.
