# Multi-LLM Comparison Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Call Claude, Gemini, and DeepSeek in parallel for every news event, log each provider's decision and latency to SQLite, use Claude's decision to drive trades, and display per-provider comparisons in the analytics page.

**Architecture:** A new `MultiLLMAdvisor` fans out to all 3 providers via `asyncio.gather`, returns a `MultiDecision(primary=claude_result, all_results=[...])`. `NewsHandler` detects the result type with `isinstance` and records 3 `llm_decisions` rows (one per provider) before routing Claude's decision to the order executor. The analytics `/api/decision/{id}` endpoint returns all sibling decisions for the same news event; the frontend expand panel renders them as a comparison table.

**Tech Stack:** Python asyncio, SQLite (via existing `TradeDB`), FastAPI + Plotly (analytics), existing `ClaudeProvider`/`GeminiProvider`/`DeepSeekProvider`

---

## File Map

| Action | Path | Responsibility |
|--------|------|----------------|
| Create | `llm/multi_advisor.py` | `ProviderResult`, `MultiDecision`, `MultiLLMAdvisor` |
| Create | `tests/test_multi_advisor.py` | Unit tests for `MultiLLMAdvisor` |
| Modify | `analytics/db.py` | Add `provider`/`latency_sec` columns + migration; extend `record_decision()` |
| Modify | `tests/test_analytics_db.py` | Tests for new `record_decision()` params |
| Modify | `config.py` | Accept `"multi"` as valid `LLM_PROVIDER`; require all 3 API keys when `multi` |
| Modify | `news/news_handler.py` | Handle `MultiDecision` return type; record 3 DB rows |
| Modify | `main.py` | Instantiate `MultiLLMAdvisor` when `LLM_PROVIDER=multi` |
| Modify | `analytics/server.py` | Rewrite `_query_decision()`; add 2 charts; update JS expand panel |
| Modify | `tests/test_analytics_server.py` | Update schema fixture; update existing test; add sibling-decision test |

---

### Task 1: Create feature branch

- [ ] **Step 1: Create and switch to branch**

```bash
git checkout -b feat/multi-llm-comparison
```

Expected: `Switched to a new branch 'feat/multi-llm-comparison'`

---

### Task 2: DB schema — add `provider` and `latency_sec` (TDD)

**Files:**
- Modify: `tests/test_analytics_db.py`
- Modify: `analytics/db.py`

- [ ] **Step 1: Write failing tests**

Append to `tests/test_analytics_db.py`:

```python
def test_record_decision_stores_provider_and_latency(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(
        nid, "2026-01-01T00:00:01Z", "buy", "AAPL", "reason", 0.9, 2,
        provider="claude", latency_sec=1.23,
    )
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] == "claude"
    assert abs(row[1] - 1.23) < 0.001


def test_record_decision_provider_defaults_to_none(db):
    nid = db.record_news("2026-01-01T00:00:00Z", "headline", None, [])
    did = db.record_decision(nid, "2026-01-01T00:00:01Z", "hold", None, "reason")
    row = db._conn.execute(
        "SELECT provider, latency_sec FROM llm_decisions WHERE id=?", (did,)
    ).fetchone()
    assert row[0] is None
    assert row[1] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_analytics_db.py::test_record_decision_stores_provider_and_latency tests/test_analytics_db.py::test_record_decision_provider_defaults_to_none -v
```

Expected: FAIL — `TypeError: record_decision() got an unexpected keyword argument 'provider'`

- [ ] **Step 3: Add migrations in `analytics/db.py`**

In `_create_tables()`, find the `for ddl in [...]` migration list and add two entries:

```python
for ddl in [
    "ALTER TABLE trades ADD COLUMN fill_latency_sec REAL",
    "ALTER TABLE llm_decisions ADD COLUMN confidence REAL DEFAULT 0.0",
    "ALTER TABLE llm_decisions ADD COLUMN hold_hours INTEGER DEFAULT 0",
    "ALTER TABLE trades ADD COLUMN hold_hours INTEGER DEFAULT 0",
    "ALTER TABLE llm_decisions ADD COLUMN provider TEXT",        # new
    "ALTER TABLE llm_decisions ADD COLUMN latency_sec REAL",    # new
]:
```

- [ ] **Step 4: Update `record_decision()` signature and INSERT**

Replace the `record_decision` method body:

```python
def record_decision(
    self,
    news_event_id: int | None,
    ts: str,
    action: str,
    ticker: str | None,
    reasoning: str,
    confidence: float = 0.0,
    hold_hours: int = 0,
    provider: str | None = None,
    latency_sec: float | None = None,
) -> int:
    cur = self._conn.execute(
        "INSERT INTO llm_decisions "
        "(news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec),
    )
    self._conn.commit()
    return cur.lastrowid  # type: ignore[return-value]
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
python -m pytest tests/test_analytics_db.py -v
```

Expected: all 10 tests PASS (8 existing + 2 new)

- [ ] **Step 6: Commit**

```bash
git add analytics/db.py tests/test_analytics_db.py
git commit -m "feat: add provider and latency_sec columns to llm_decisions"
```

---

### Task 3: Config — accept `LLM_PROVIDER=multi`

**Files:**
- Modify: `config.py`

- [ ] **Step 1: Update provider validation**

In `load_config()`, replace:

```python
provider = os.getenv("LLM_PROVIDER", "claude").lower()
if provider not in ("claude", "gemini", "deepseek"):
    raise ValueError(f"LLM_PROVIDER must be 'claude', 'gemini', or 'deepseek', got {provider!r}")

# ALPACA_API_KEY/SECRET_KEY are still required — used by NewsDataStream (news feed only, not trading)
required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID"]
if provider == "claude":
    required.append("ANTHROPIC_API_KEY")
elif provider == "gemini":
    required.append("GOOGLE_API_KEY")
else:
    required.append("DEEPSEEK_API_KEY")
```

With:

```python
provider = os.getenv("LLM_PROVIDER", "claude").lower()
if provider not in ("claude", "gemini", "deepseek", "multi"):
    raise ValueError(
        f"LLM_PROVIDER must be 'claude', 'gemini', 'deepseek', or 'multi', got {provider!r}"
    )

# ALPACA_API_KEY/SECRET_KEY are still required — used by NewsDataStream (news feed only, not trading)
required = ["ALPACA_API_KEY", "ALPACA_SECRET_KEY", "TRADIER_ACCESS_TOKEN", "TRADIER_ACCOUNT_ID"]
if provider == "claude":
    required.append("ANTHROPIC_API_KEY")
elif provider == "gemini":
    required.append("GOOGLE_API_KEY")
elif provider == "deepseek":
    required.append("DEEPSEEK_API_KEY")
else:  # multi — all three providers are active
    required.extend(["ANTHROPIC_API_KEY", "GOOGLE_API_KEY", "DEEPSEEK_API_KEY"])
```

- [ ] **Step 2: Verify import still works**

```bash
python -c "import config; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Commit**

```bash
git add config.py
git commit -m "feat: add 'multi' as valid LLM_PROVIDER option requiring all three API keys"
```

---

### Task 4: `MultiLLMAdvisor` (TDD)

**Files:**
- Create: `tests/test_multi_advisor.py`
- Create: `llm/multi_advisor.py`

- [ ] **Step 1: Write failing tests**

Create `tests/test_multi_advisor.py`:

```python
import asyncio
from unittest.mock import AsyncMock
import pytest
from llm.multi_advisor import MultiDecision, MultiLLMAdvisor, ProviderResult
from llm.llm_advisor import Decision


def _make_advisor(claude_response: str, gemini_response: str, deepseek_response: str) -> MultiLLMAdvisor:
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = claude_response
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = gemini_response
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = deepseek_response
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock
    return advisor


_BUY_JSON = '{"action":"buy","ticker":"AAPL","reasoning":"strong earnings","confidence":0.9,"hold_hours":2}'
_HOLD_JSON = '{"action":"hold","ticker":null,"reasoning":"unsure","confidence":0.0,"hold_hours":0}'


def test_primary_is_always_claude_decision():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert isinstance(result, MultiDecision)
    assert result.primary.action == "buy"
    assert result.primary.ticker == "AAPL"


def test_all_results_ordered_claude_gemini_deepseek():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    providers = [r.provider for r in result.all_results]
    assert providers == ["claude", "gemini", "deepseek"]


def test_claude_error_falls_back_to_hold():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.side_effect = RuntimeError("API down")
    gemini_mock = AsyncMock()
    gemini_mock.complete.return_value = _HOLD_JSON
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _HOLD_JSON
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock

    result = asyncio.run(advisor.analyze("headline", "summary", [], set(), set(), 0.0))
    assert result.primary.action == "hold"
    assert "error" in result.primary.reasoning.lower()


def test_latency_sec_always_non_negative():
    advisor = _make_advisor(_BUY_JSON, _HOLD_JSON, _HOLD_JSON)
    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    for pr in result.all_results:
        assert isinstance(pr, ProviderResult)
        assert pr.latency_sec >= 0.0


def test_partial_provider_error_still_returns_three_results():
    advisor = object.__new__(MultiLLMAdvisor)
    claude_mock = AsyncMock()
    claude_mock.complete.return_value = _BUY_JSON
    gemini_mock = AsyncMock()
    gemini_mock.complete.side_effect = RuntimeError("timeout")
    deepseek_mock = AsyncMock()
    deepseek_mock.complete.return_value = _HOLD_JSON
    advisor._claude = claude_mock
    advisor._gemini = gemini_mock
    advisor._deepseek = deepseek_mock

    result = asyncio.run(advisor.analyze("headline", "summary", ["AAPL"], set(), set(), 0.0))
    assert len(result.all_results) == 3
    assert result.all_results[1].provider == "gemini"
    assert result.all_results[1].decision.action == "hold"
    assert "error" in result.all_results[1].decision.reasoning.lower()
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
python -m pytest tests/test_multi_advisor.py -v
```

Expected: FAIL — `ModuleNotFoundError: No module named 'llm.multi_advisor'`

- [ ] **Step 3: Implement `llm/multi_advisor.py`**

Create `llm/multi_advisor.py`:

```python
import asyncio
import time
from dataclasses import dataclass

from llm.llm_advisor import Decision, _PROMPT_TEMPLATE, _parse_response
from llm.providers import ClaudeProvider, DeepSeekProvider, GeminiProvider


@dataclass
class ProviderResult:
    provider: str
    decision: Decision
    latency_sec: float


@dataclass
class MultiDecision:
    primary: Decision
    all_results: list[ProviderResult]


class MultiLLMAdvisor:
    def __init__(self, config) -> None:
        self._claude = ClaudeProvider(config.anthropic_api_key, config.anthropic_model)
        self._gemini = GeminiProvider(config.google_api_key, config.gemini_model)
        self._deepseek = DeepSeekProvider(config.deepseek_api_key, config.deepseek_model)

    async def _call(self, provider_name: str, provider, prompt: str) -> ProviderResult:
        start = time.monotonic()
        try:
            text = await provider.complete(prompt)
            decision = _parse_response(text)
        except Exception as exc:
            decision = Decision(action="hold", ticker=None, reasoning=f"error: {exc}")
        return ProviderResult(
            provider=provider_name,
            decision=decision,
            latency_sec=time.monotonic() - start,
        )

    async def analyze(
        self,
        headline: str,
        summary: str,
        symbols: list[str],
        held_tickers: set[str],
        shorted_tickers: set[str],
        news_age_hours: float = 0.0,
    ) -> MultiDecision:
        prompt = _PROMPT_TEMPLATE.format(
            headline=headline,
            summary=summary or "(no summary)",
            symbols=", ".join(symbols) if symbols else "none",
            held_tickers=", ".join(held_tickers) if held_tickers else "none",
            shorted_tickers=", ".join(shorted_tickers) if shorted_tickers else "none",
            news_age_hours=news_age_hours,
        )
        results: list[ProviderResult] = list(
            await asyncio.gather(
                self._call("claude", self._claude, prompt),
                self._call("gemini", self._gemini, prompt),
                self._call("deepseek", self._deepseek, prompt),
            )
        )
        return MultiDecision(primary=results[0].decision, all_results=results)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
python -m pytest tests/test_multi_advisor.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add llm/multi_advisor.py tests/test_multi_advisor.py
git commit -m "feat: add MultiLLMAdvisor that fans out to all three LLM providers in parallel"
```

---

### Task 5: `NewsHandler` — handle `MultiDecision`

**Files:**
- Modify: `news/news_handler.py`

- [ ] **Step 1: Add import at the top of `news/news_handler.py`**

After the existing `from llm.llm_advisor import LLMAdvisor` line, add:

```python
from llm.multi_advisor import MultiDecision, MultiLLMAdvisor
```

- [ ] **Step 2: Update `__init__` type annotation**

Replace:

```python
        llm_advisor: LLMAdvisor,
```

With:

```python
        llm_advisor: LLMAdvisor | MultiLLMAdvisor,
```

- [ ] **Step 3: Update `_handle_news` to split result into `decision` and record all providers**

In `_handle_news`, replace this exact block:

```python
            # Capture decision timestamp before analyzing
            decision_monotonic = time.monotonic()
            decision = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
                news_age_hours=age_hours,
            )

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            decision_id: int | None = None
            if self._db is not None and news_event_id is not None:
                try:
                    decision_ts = datetime.now(timezone.utc).isoformat()
                    decision_id = await asyncio.to_thread(
                        self._db.record_decision,
                        news_event_id, decision_ts, decision.action, decision.ticker, decision.reasoning,
                        decision.confidence, decision.hold_hours,
                    )
                except Exception as db_err:
                    logger.warning("Failed to record LLM decision in analytics DB: %s", db_err)
```

With:

```python
            # Capture decision timestamp before analyzing
            decision_monotonic = time.monotonic()
            result = await self._advisor.analyze(
                headline=headline,
                summary=summary,
                symbols=symbols,
                held_tickers=self._executor.held_tickers,
                shorted_tickers=self._executor.shorted_tickers,
                news_age_hours=age_hours,
            )

            if isinstance(result, MultiDecision):
                decision = result.primary
            else:
                decision = result

            logger.info("LLM decision: %s %s — %s", decision.action, decision.ticker, decision.reasoning)

            decision_id: int | None = None
            if self._db is not None and news_event_id is not None:
                try:
                    decision_ts = datetime.now(timezone.utc).isoformat()
                    if isinstance(result, MultiDecision):
                        for pr in result.all_results:
                            row_id = await asyncio.to_thread(
                                self._db.record_decision,
                                news_event_id, decision_ts,
                                pr.decision.action, pr.decision.ticker, pr.decision.reasoning,
                                pr.decision.confidence, pr.decision.hold_hours,
                                pr.provider, pr.latency_sec,
                            )
                            if pr.provider == "claude":
                                decision_id = row_id
                    else:
                        decision_id = await asyncio.to_thread(
                            self._db.record_decision,
                            news_event_id, decision_ts, decision.action, decision.ticker, decision.reasoning,
                            decision.confidence, decision.hold_hours,
                        )
                except Exception as db_err:
                    logger.warning("Failed to record LLM decision in analytics DB: %s", db_err)
```

- [ ] **Step 4: Verify imports still work**

```bash
python -c "from news.news_handler import NewsHandler; print('OK')"
```

Expected: `OK`

- [ ] **Step 5: Commit**

```bash
git add news/news_handler.py
git commit -m "feat: NewsHandler records all provider decisions when MultiLLMAdvisor is used"
```

---

### Task 6: Wire `MultiLLMAdvisor` in `main.py`

**Files:**
- Modify: `main.py`

- [ ] **Step 1: Replace the advisor instantiation in `main()`**

In `main.py`, replace:

```python
        llm_advisor = LLMAdvisor(config)
        news_handler = NewsHandler(client, config, llm_advisor, order_executor, db)
```

With:

```python
        if config.llm_provider == "multi":
            from llm.multi_advisor import MultiLLMAdvisor
            llm_advisor = MultiLLMAdvisor(config)
        else:
            llm_advisor = LLMAdvisor(config)
        news_handler = NewsHandler(client, config, llm_advisor, order_executor, db)
```

- [ ] **Step 2: Verify import works**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 3: Run full test suite to verify nothing broke**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 4: Commit**

```bash
git add main.py
git commit -m "feat: instantiate MultiLLMAdvisor in main when LLM_PROVIDER=multi"
```

---

### Task 7: Analytics server — new endpoint format, charts, and frontend (TDD)

**Files:**
- Modify: `tests/test_analytics_server.py`
- Modify: `analytics/server.py`

- [ ] **Step 1: Update `_make_db()` fixture in `tests/test_analytics_server.py`**

Replace the `llm_decisions` table definition inside `_make_db()`:

```python
        CREATE TABLE llm_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_event_id INTEGER REFERENCES news_events(id),
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ticker TEXT,
            reasoning TEXT,
            confidence REAL DEFAULT 0.0,
            hold_hours INTEGER DEFAULT 0,
            provider TEXT,
            latency_sec REAL
        );
```

- [ ] **Step 2: Update `test_query_decision_returns_fields` to match new response format**

Replace the existing `test_query_decision_returns_fields` test entirely:

```python
def test_query_decision_returns_fields():
    con = _make_db()
    con.execute(
        "INSERT INTO news_events (ts, headline) VALUES ('2026-01-01T10:00:00', 'Fed raises rates')"
    )
    con.execute(
        "INSERT INTO llm_decisions (news_event_id, ts, action, ticker, reasoning, confidence, hold_hours) "
        "VALUES (1, '2026-01-01T10:00:01', 'buy', 'JPM', 'Banks benefit from rate hikes', 0.85, 2)"
    )
    con.commit()
    result = _query_decision(con, 1)
    assert result is not None
    assert result["headline"] == "Fed raises rates"
    assert result["ts"] == "2026-01-01T10:00:00"
    assert len(result["decisions"]) == 1
    d = result["decisions"][0]
    assert d["action"] == "buy"
    assert d["ticker"] == "JPM"
    assert d["confidence"] == pytest.approx(0.85)
    assert d["reasoning"] == "Banks benefit from rate hikes"
    assert d["hold_hours"] == 2
```

- [ ] **Step 3: Add test for sibling decision lookup**

Append to `tests/test_analytics_server.py`:

```python
def test_query_decision_returns_all_siblings():
    con = _make_db()
    con.execute(
        "INSERT INTO news_events (ts, headline) VALUES ('2026-01-01T10:00:00', 'AAPL beats earnings')"
    )
    con.executemany(
        "INSERT INTO llm_decisions "
        "(news_event_id, ts, action, ticker, reasoning, confidence, hold_hours, provider, latency_sec) "
        "VALUES (1, '2026-01-01T10:00:01', ?, ?, ?, ?, ?, ?, ?)",
        [
            ("buy",  "AAPL", "strong",  0.90, 2, "claude",   1.2),
            ("hold", None,   "unsure",  0.00, 0, "gemini",   0.8),
            ("buy",  "AAPL", "bullish", 0.75, 1, "deepseek", 2.1),
        ],
    )
    con.commit()

    result = _query_decision(con, 1)  # query using Claude's id
    assert result is not None
    assert result["headline"] == "AAPL beats earnings"
    assert len(result["decisions"]) == 3
    providers = [d["provider"] for d in result["decisions"]]
    assert providers == ["claude", "gemini", "deepseek"]
    assert result["decisions"][0]["action"] == "buy"
    assert result["decisions"][1]["action"] == "hold"
    assert abs(result["decisions"][0]["latency_sec"] - 1.2) < 0.001
```

- [ ] **Step 4: Run tests to verify they fail**

```bash
python -m pytest tests/test_analytics_server.py -v
```

Expected: FAIL on `test_query_decision_returns_fields` and `test_query_decision_returns_all_siblings` (wrong response shape)

- [ ] **Step 5: Rewrite `_query_decision()` in `analytics/server.py`**

Replace the existing `_query_decision` function:

```python
def _query_decision(con: sqlite3.Connection, decision_id: int) -> dict | None:
    row = con.execute(
        "SELECT news_event_id FROM llm_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    if row is None:
        return None
    news_event_id = row["news_event_id"]

    headline_row = con.execute(
        "SELECT headline, ts FROM news_events WHERE id = ?", (news_event_id,)
    ).fetchone()

    decision_rows = con.execute(
        "SELECT provider, action, ticker, confidence, hold_hours, reasoning, latency_sec "
        "FROM llm_decisions WHERE news_event_id = ? "
        "ORDER BY CASE provider "
        "WHEN 'claude' THEN 0 WHEN 'gemini' THEN 1 WHEN 'deepseek' THEN 2 ELSE 3 END",
        (news_event_id,),
    ).fetchall()

    return {
        "headline": headline_row["headline"] if headline_row else None,
        "ts": headline_row["ts"] if headline_row else None,
        "decisions": [dict(d) for d in decision_rows],
    }
```

- [ ] **Step 6: Run tests to verify they pass**

```bash
python -m pytest tests/test_analytics_server.py -v
```

Expected: all 6 tests PASS

- [ ] **Step 7: Add two new charts in `_query_charts()`**

At the end of `_query_charts()`, before the `charts = {...}` dict, add:

```python
    # 11: Provider response latency (box plot)
    plat_rows = con.execute(
        "SELECT provider, latency_sec FROM llm_decisions "
        "WHERE provider IS NOT NULL AND latency_sec IS NOT NULL"
    ).fetchall()
    fig_plat = go.Figure()
    for p in ["claude", "gemini", "deepseek"]:
        vals = [r["latency_sec"] for r in plat_rows if r["provider"] == p]
        if vals:
            fig_plat.add_trace(go.Box(y=vals, name=p))
    fig_plat.update_layout(title="Provider Response Latency", yaxis_title="Seconds")

    # 12: LLM agreement rate (events where all 3 providers agreed on the same action)
    agree_rows = con.execute(
        "SELECT COUNT(DISTINCT action) AS unique_actions "
        "FROM llm_decisions WHERE provider IS NOT NULL "
        "GROUP BY news_event_id HAVING COUNT(*) = 3"
    ).fetchall()
    agreed = sum(1 for r in agree_rows if r["unique_actions"] == 1)
    disagreed = len(agree_rows) - agreed
    fig_agree = go.Figure(go.Bar(x=["Agreed", "Disagreed"], y=[agreed, disagreed]))
    fig_agree.update_layout(
        title="LLM Agreement Rate (3-provider events)", yaxis_title="News Events"
    )
```

Then add the two keys to the `charts` dict:

```python
    charts = {
        "cumulative": _fig_json(fig_cum),
        "daily": _fig_json(fig_daily),
        "exit": _fig_json(fig_exit),
        "dist": _fig_json(fig_dist),
        "duration": _fig_json(fig_dur),
        "actions": _fig_json(fig_actions),
        "win_rate": _fig_json(fig_wr),
        "pnl_hour": _fig_json(fig_hour),
        "conf_outcome": _fig_json(fig_conf),
        "latency_trend": _fig_json(fig_lat),
        "provider_latency": _fig_json(fig_plat),   # new
        "agreement_rate": _fig_json(fig_agree),    # new
    }
```

- [ ] **Step 8: Add CSS for the comparison table**

In the `<style>` block inside `index()`, add before the closing `</style>`:

```css
  .provider-compare { border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 12px; }
  .provider-compare th, .provider-compare td { border: 1px solid #ddd; padding: 5px 8px; text-align: left; vertical-align: top; }
  .provider-compare th { background: #e8e8e8; font-weight: 600; }
  .provider-compare td:first-child { font-weight: 600; white-space: nowrap; }
  .provider-compare td { max-width: 280px; white-space: pre-wrap; word-break: break-word; }
```

- [ ] **Step 9: Update the JS expand panel to render comparison table**

Replace the entire `fetch('/api/decision/' + decisionId)` block in the `<script>` section:

```javascript
  document.querySelector('#trades-table tbody').addEventListener('click', function(e) {{
    const row = e.target.closest('tr.trade-row');
    if (!row) return;
    const next = row.nextElementSibling;
    if (next && next.classList.contains('detail-row')) {{
      next.remove();
      row.querySelector('.expand-btn').textContent = '▶';
      return;
    }}
    const decisionId = row.dataset.decisionId;
    if (!decisionId) return;
    fetch('/api/decision/' + decisionId)
      .then(r => r.json())
      .then(d => {{
        const esc = s => (s || '').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
        const decisions = d.decisions || [];
        let providerHtml = '';
        if (decisions.length > 1) {{
          const heads = decisions.map(p => '<th>' + esc(p.provider || '?') + '</th>').join('');
          const rows2 = [
            ['Action',     decisions.map(p => esc(p.action || '—'))],
            ['Ticker',     decisions.map(p => esc(p.ticker || '—'))],
            ['Confidence', decisions.map(p => p.confidence != null ? p.confidence.toFixed(2) : '—')],
            ['Hold',       decisions.map(p => p.hold_hours ? p.hold_hours + 'h' : '—')],
            ['Latency',    decisions.map(p => p.latency_sec != null ? p.latency_sec.toFixed(2) + 's' : '—')],
            ['Reasoning',  decisions.map(p => esc(p.reasoning || ''))],
          ];
          const bodyRows = rows2.map(([label, cells]) =>
            '<tr><td>' + label + '</td>' + cells.map(c => '<td>' + c + '</td>').join('') + '</tr>'
          ).join('');
          providerHtml = '<table class="provider-compare"><thead><tr><th></th>' + heads + '</tr></thead><tbody>' + bodyRows + '</tbody></table>';
        }} else if (decisions.length === 1) {{
          const dec = decisions[0];
          const conf = dec.confidence != null ? dec.confidence.toFixed(2) : '—';
          const hold = dec.hold_hours ? dec.hold_hours + 'h' : '—';
          providerHtml =
            '<div class="meta">confidence: ' + conf + ' &nbsp;|&nbsp; hold: ' + hold + '</div>' +
            '<div class="reasoning">' + esc(dec.reasoning) + '</div>';
        }}
        const detail = document.createElement('tr');
        detail.className = 'detail-row';
        detail.innerHTML =
          '<td colspan="8">' +
          '<strong>' + esc(d.headline) + '</strong>' +
          '<div class="meta">' + esc(d.ts) + '</div>' +
          providerHtml +
          '</td>';
        row.after(detail);
        row.querySelector('.expand-btn').textContent = '▼';
      }});
  }});
```

- [ ] **Step 10: Run full test suite**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 11: Commit**

```bash
git add analytics/server.py tests/test_analytics_server.py
git commit -m "feat: analytics shows per-provider decisions, latency, and agreement rate chart"
```

---

### Task 8: Final verification and branch ready

- [ ] **Step 1: Verify all modules import cleanly**

```bash
python -c "import main; print('OK')"
```

Expected: `OK`

- [ ] **Step 2: Run full test suite one last time**

```bash
python -m pytest tests/ -v
```

Expected: all tests PASS

- [ ] **Step 3: Review git log**

```bash
git log feat/multi-llm-comparison --oneline
```

Expected: 6 commits on the branch (branch, schema, config, advisor, news handler, main, analytics)
