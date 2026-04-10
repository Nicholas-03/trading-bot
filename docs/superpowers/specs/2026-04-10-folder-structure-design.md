# Folder Structure Redesign

**Date:** 2026-04-10

## Goal

Reorganize the flat root-level Python source files into domain-specific feature folders to improve navigability and make each module's responsibility immediately obvious.

## Constraints

- Keep flat-style imports (no top-level package namespace like `trading_bot.x`)
- `config.py` and `main.py` remain at the root
- No change to test structure (tests stay flat in `tests/`)

## Target Structure

```
trading-bot/
├── trading/
│   ├── __init__.py
│   ├── order_executor.py
│   └── position_monitor.py
├── llm/
│   ├── __init__.py
│   └── llm_advisor.py
├── news/
│   ├── __init__.py
│   └── news_handler.py
├── config.py
├── main.py
├── tests/
│   ├── __init__.py
│   ├── test_llm_advisor.py
│   └── test_position_monitor.py
├── docs/
├── .env
├── .env.example
└── requirements.txt
```

## Import Changes

All cross-module imports updated to use folder prefix:

| Old import | New import |
|---|---|
| `from order_executor import OrderExecutor` | `from trading.order_executor import OrderExecutor` |
| `from position_monitor import PositionMonitor, compute_pnl_pct` | `from trading.position_monitor import PositionMonitor, compute_pnl_pct` |
| `from llm_advisor import LLMAdvisor` | `from llm.llm_advisor import LLMAdvisor` |
| `from news_handler import NewsHandler` | `from news.news_handler import NewsHandler` |

## Files to Create

- `trading/__init__.py` (empty)
- `llm/__init__.py` (empty)
- `news/__init__.py` (empty)

## Files to Move

- `order_executor.py` → `trading/order_executor.py`
- `position_monitor.py` → `trading/position_monitor.py`
- `llm_advisor.py` → `llm/llm_advisor.py`
- `news_handler.py` → `news/news_handler.py`

## Files Unchanged

- `config.py` — shared by all modules, stays at root
- `main.py` — entry point, stays at root
- `tests/` — flat, no subfolders needed

## CLAUDE.md Updates

The key files table and architecture section in `CLAUDE.md` must be updated to reflect new paths.
