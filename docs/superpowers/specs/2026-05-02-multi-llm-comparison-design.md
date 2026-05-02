# Multi-LLM Comparison Design

**Date:** 2026-05-02  
**Status:** Approved

## Overview

Call all three LLMs (Claude, Gemini, DeepSeek) in parallel for every news event. Log each provider's decision and response latency to the DB. Use Claude's decision to drive actual trades. Show per-provider comparisons in the analytics page.

## Schema Changes (`analytics/db.py`)

Add two columns to `llm_decisions` via the existing migration pattern (try/except `ALTER TABLE`):

- `provider TEXT` — one of `"claude"`, `"gemini"`, `"deepseek"`
- `latency_sec REAL` — wall-clock seconds from prompt send to response received

Each news event produces **3 `llm_decisions` rows** (one per LLM). The trade row's `decision_id` points to Claude's row only. The other two rows are stored for analytics but not linked to trades.

`TradeDB.record_decision()` gains two optional parameters: `provider: str | None = None` and `latency_sec: float | None = None`. Existing callers are unaffected.

## New Class: `MultiLLMAdvisor` (`llm/multi_advisor.py`)

Imports `_PROMPT_TEMPLATE` and `_parse_response` directly from `llm.llm_advisor` — no duplication. Both are already module-level in that file.

Holds one instance of each provider: `ClaudeProvider`, `GeminiProvider`, `DeepSeekProvider`.

### Data structures

```python
@dataclass
class ProviderResult:
    provider: str        # "claude" | "gemini" | "deepseek"
    decision: Decision
    latency_sec: float

@dataclass
class MultiDecision:
    primary: Decision              # Claude's decision — drives trades
    all_results: list[ProviderResult]  # all 3, always in order [claude, gemini, deepseek]
```

### `analyze()` behaviour

1. Build the prompt once (same `_PROMPT_TEMPLATE` as `LLMAdvisor`).
2. Call all 3 providers via `asyncio.gather(..., return_exceptions=True)`, each wrapped in a timing helper that records `latency_sec`.
3. Parse each response with `_parse_response()`. On any error (exception or parse failure), fall back to `Decision(action="hold", ticker=None, reasoning="<error>")` with `latency_sec` still recorded.
4. Claude's result is always `primary`, regardless of what other providers return.
5. Return `MultiDecision`.

### Config

- New `LLM_PROVIDER=multi` value in `.env`.
- When `multi`, validation requires all three keys: `ANTHROPIC_API_KEY`, `GOOGLE_API_KEY`, `DEEPSEEK_API_KEY`.
- Single-provider mode (`claude`/`gemini`/`deepseek`) is completely unchanged.
- `config.py` validation updated to allow `"multi"` as a valid provider value.

## `NewsHandler` Changes (`news/news_handler.py`)

`NewsHandler.__init__` accepts `LLMAdvisor | MultiLLMAdvisor` (union type annotation; no runtime interface needed).

`_handle_news()` uses `isinstance(result, MultiDecision)` to branch:

1. Call `self._advisor.analyze(...)` — returns either `Decision` (single mode) or `MultiDecision` (multi mode).
2. If `isinstance(result, MultiDecision)`:
   - Record all 3 `ProviderResult`s to DB in order, each with `provider` and `latency_sec` set.
   - Capture the `decision_id` of Claude's row for trade linking.
   - Set `decision = result.primary` for downstream logic.
3. Else (plain `Decision`, single-provider mode):
   - Existing behaviour unchanged — one DB row, `provider=None`, `latency_sec=None`.
4. Everything downstream (confidence gate, buy/sell/short routing) operates on `decision` only — no changes there.

## `main.py` Changes

Instantiate `MultiLLMAdvisor` when `config.llm_provider == "multi"`, otherwise instantiate the existing `LLMAdvisor`. Pass the advisor to `NewsHandler` as before.

## Analytics Changes (`analytics/server.py`)

### New charts

- **Provider Response Latency** — box plot (one box per provider) from `llm_decisions` where `latency_sec IS NOT NULL`.
- **LLM Agreement Rate** — for each `news_event_id` that has exactly 3 decisions, count how many events had all 3 providers agree on the same action. Display as a bar: `agreed` vs `disagreed` count.

### Expanded row detail

`/api/decision/{decision_id}` changes: fetch all sibling decisions for the same `news_event_id` (not just the one row). Return a list ordered `[claude, gemini, deepseek]`.

The expand panel replaces the single-provider reasoning block with a 3-column comparison table:

| | Claude | Gemini | DeepSeek |
|---|---|---|---|
| Action | buy | hold | buy |
| Confidence | 0.82 | — | 0.71 |
| Latency | 1.2s | 0.9s | 2.1s |
| Reasoning | ... | ... | ... |

If fewer than 3 providers are present (e.g., one errored out), that column shows "—" for all fields.

## What Does NOT Change

- `LLMAdvisor` (single-provider class) — untouched.
- `OrderExecutor`, `PositionMonitor`, `TradierClient` — untouched.
- Trade linking logic (`decision_id` on `trades` table) — unchanged; always points to Claude's row.
- Pre-LLM filters (`is_retrospective_headline`, `is_routine_news`, staleness gate) — unchanged.
- Confidence gate — evaluated on Claude's decision only.

## Testing

- Unit tests for `MultiLLMAdvisor` in `tests/test_multi_advisor.py`: verify that Claude's decision is always `primary`; verify that provider errors produce a `hold` fallback; verify that `latency_sec` is always set (even on error).
- Unit tests for updated `record_decision()` with new optional params.
- `analytics/server.py` tests: verify `/api/decision/{id}` returns all sibling decisions.
- Existing tests are unaffected (no changes to `LLMAdvisor`, `_parse_response`, or DB record methods' existing signatures).
