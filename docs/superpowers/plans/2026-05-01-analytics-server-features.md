# Analytics Server Feature Additions Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a summary stats bar, four new Plotly charts, and an inline drill-down row to `analytics/server.py`.

**Architecture:** Charts and stats remain server-side rendered (current pattern). Drill-down detail is fetched on demand via a new `GET /api/decision/{id}` endpoint backed by a pure `_query_decision()` helper. A new `_query_stats()` helper computes the five headline metrics. `_build_charts()` is renamed `_build_page_data()` to return stats alongside charts.

**Tech Stack:** Python 3.12+, FastAPI, Plotly, SQLite (stdlib), uvicorn

---

## File Map

| File | Change |
|---|---|
| `analytics/server.py` | Add `_query_stats`, `_query_decision`, `_render_stats_bar`, `GET /api/decision/{id}`, 4 new charts, drill-down table + JS, rename `_build_charts` |
| `tests/test_analytics_server.py` | New — unit tests for `_query_stats` and `_query_decision` |

---

### Task 1: Add `_query_decision` and `/api/decision/{id}` endpoint (TDD)

**Files:**
- Create: `tests/test_analytics_server.py`
- Modify: `analytics/server.py`

- [ ] **Step 1: Create test file with DB fixture and failing tests**

Create `tests/test_analytics_server.py`:

```python
import sqlite3
import pytest
from analytics.server import _query_decision


def _make_db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:")
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE news_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts TEXT NOT NULL,
            headline TEXT NOT NULL,
            summary TEXT,
            symbols TEXT
        );
        CREATE TABLE llm_decisions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            news_event_id INTEGER REFERENCES news_events(id),
            ts TEXT NOT NULL,
            action TEXT NOT NULL,
            ticker TEXT,
            reasoning TEXT,
            confidence REAL DEFAULT 0.0,
            hold_hours INTEGER DEFAULT 0
        );
        CREATE TABLE trades (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            decision_id INTEGER REFERENCES llm_decisions(id),
            ticker TEXT NOT NULL,
            side TEXT NOT NULL,
            qty INTEGER NOT NULL,
            entry_price REAL,
            exit_price REAL,
            pnl_usd REAL,
            pnl_pct REAL,
            exit_reason TEXT,
            fill_latency_sec REAL,
            hold_hours INTEGER DEFAULT 0,
            opened_at TEXT NOT NULL,
            closed_at TEXT
        );
    """)
    return con


def test_query_decision_not_found():
    con = _make_db()
    assert _query_decision(con, 999) is None


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
    assert result["ticker"] == "JPM"
    assert result["confidence"] == pytest.approx(0.85)
    assert result["reasoning"] == "Banks benefit from rate hikes"
    assert result["hold_hours"] == 2
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_analytics_server.py -v
```

Expected: `ImportError` or `AttributeError` — `_query_decision` not yet defined.

- [ ] **Step 3: Add `_query_decision` to `analytics/server.py`**

Add after the `_fig_json` function (around line 27):

```python
def _query_decision(con: sqlite3.Connection, decision_id: int) -> dict | None:
    row = con.execute(
        "SELECT n.headline, n.ts, d.action, d.ticker, d.confidence, d.hold_hours, d.reasoning "
        "FROM llm_decisions d JOIN news_events n ON n.id = d.news_event_id "
        "WHERE d.id = ?",
        (decision_id,),
    ).fetchone()
    return dict(row) if row is not None else None
```

- [ ] **Step 4: Add the FastAPI endpoint**

Add after the `index()` route at the bottom of `analytics/server.py` (before `if __name__ == "__main__":`):

```python
from fastapi.responses import JSONResponse


@app.get("/api/decision/{decision_id}")
def get_decision(decision_id: int):
    con = _conn()
    try:
        result = _query_decision(con, decision_id)
    finally:
        con.close()
    if result is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return result
```

Note: move the `JSONResponse` import to the top of the file alongside the existing `HTMLResponse` import:
```python
from fastapi.responses import HTMLResponse, JSONResponse
```

- [ ] **Step 5: Run tests to verify they pass**

```
python -m pytest tests/test_analytics_server.py -v
```

Expected:
```
tests/test_analytics_server.py::test_query_decision_not_found PASSED
tests/test_analytics_server.py::test_query_decision_returns_fields PASSED
```

- [ ] **Step 6: Commit**

```
git add tests/test_analytics_server.py analytics/server.py
git commit -m "feat: add _query_decision and /api/decision/{id} endpoint"
```

---

### Task 2: Add `_query_stats` (TDD)

**Files:**
- Modify: `tests/test_analytics_server.py`
- Modify: `analytics/server.py`

- [ ] **Step 1: Add failing tests to `tests/test_analytics_server.py`**

Add these imports and tests at the bottom of the existing test file:

```python
from analytics.server import _query_stats


def test_query_stats_empty_db():
    con = _make_db()
    stats = _query_stats(con)
    assert stats["total"] == 0
    assert stats["win_rate"] == 0.0
    assert stats["total_pnl"] == 0.0
    assert stats["best"] is None
    assert stats["worst"] is None


def test_query_stats_with_trades():
    con = _make_db()
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('AAPL', 'buy', 10, 50.0, '2026-01-01', '2026-01-01')"
    )
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('GOOG', 'buy', 5, -20.0, '2026-01-02', '2026-01-02')"
    )
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at, closed_at) "
        "VALUES ('MSFT', 'buy', 3, 10.0, '2026-01-03', '2026-01-03')"
    )
    con.commit()
    stats = _query_stats(con)
    assert stats["total"] == 3
    assert stats["win_rate"] == pytest.approx(66.67, rel=0.01)
    assert stats["total_pnl"] == pytest.approx(40.0)
    assert stats["best"] == ("AAPL", 50.0)
    assert stats["worst"] == ("GOOG", -20.0)


def test_query_stats_ignores_open_trades():
    con = _make_db()
    # Trade with no closed_at is still open — must not count
    con.execute(
        "INSERT INTO trades (ticker, side, qty, pnl_usd, opened_at) "
        "VALUES ('AAPL', 'buy', 10, 50.0, '2026-01-01')"
    )
    con.commit()
    stats = _query_stats(con)
    assert stats["total"] == 0
    assert stats["best"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```
python -m pytest tests/test_analytics_server.py -v
```

Expected: `ImportError` — `_query_stats` not yet defined.

- [ ] **Step 3: Add `_query_stats` to `analytics/server.py`**

Add directly after `_query_decision` (the function added in Task 1):

```python
def _query_stats(con: sqlite3.Connection) -> dict:
    row = con.execute(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) AS wins, "
        "SUM(pnl_usd) AS total_pnl "
        "FROM trades WHERE closed_at IS NOT NULL"
    ).fetchone()
    total = row["total"] or 0
    wins = row["wins"] or 0
    total_pnl = row["total_pnl"] or 0.0
    win_rate = (wins / total * 100) if total > 0 else 0.0

    best = con.execute(
        "SELECT ticker, pnl_usd FROM trades "
        "WHERE closed_at IS NOT NULL AND pnl_usd IS NOT NULL "
        "ORDER BY pnl_usd DESC LIMIT 1"
    ).fetchone()
    worst = con.execute(
        "SELECT ticker, pnl_usd FROM trades "
        "WHERE closed_at IS NOT NULL AND pnl_usd IS NOT NULL "
        "ORDER BY pnl_usd ASC LIMIT 1"
    ).fetchone()

    return {
        "total": total,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "best": (best["ticker"], best["pnl_usd"]) if best else None,
        "worst": (worst["ticker"], worst["pnl_usd"]) if worst else None,
    }
```

- [ ] **Step 4: Run tests to verify they pass**

```
python -m pytest tests/test_analytics_server.py -v
```

Expected: all 5 tests pass.

- [ ] **Step 5: Commit**

```
git add tests/test_analytics_server.py analytics/server.py
git commit -m "feat: add _query_stats with tests"
```

---

### Task 3: Wire stats into the page — rename `_build_charts`, add stats bar

**Files:**
- Modify: `analytics/server.py`

- [ ] **Step 1: Rename `_build_charts` to `_build_page_data` and thread stats through**

Replace the current `_build_charts` function:

```python
def _build_page_data() -> tuple[dict, dict, list[dict]]:
    con = _conn()
    try:
        charts, recent = _query_charts(con)
        stats = _query_stats(con)
        return charts, stats, recent
    finally:
        con.close()
```

- [ ] **Step 2: Update `index()` to use the new signature**

Change the first line of `index()` from:

```python
charts, recent = _build_charts()
```

to:

```python
charts, stats, recent = _build_page_data()
```

- [ ] **Step 3: Add `_render_stats_bar` helper**

Add after `_query_stats`:

```python
def _render_stats_bar(stats: dict) -> str:
    pnl_class = "pos" if stats["total_pnl"] >= 0 else "neg"
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    best_str = f"{stats['best'][0]} {stats['best'][1]:+.2f}" if stats["best"] else "—"
    worst_str = f"{stats['worst'][0]} {stats['worst'][1]:+.2f}" if stats["worst"] else "—"
    return (
        '<div class="stats-bar">'
        f'<div class="stat-card"><div class="label">Closed Trades</div>'
        f'<div class="value">{stats["total"]}</div></div>'
        f'<div class="stat-card"><div class="label">Win Rate</div>'
        f'<div class="value">{stats["win_rate"]:.1f}%</div></div>'
        f'<div class="stat-card"><div class="label">Total P&amp;L</div>'
        f'<div class="value {pnl_class}">{pnl_sign}{stats["total_pnl"]:.2f}</div></div>'
        f'<div class="stat-card"><div class="label">Best Trade</div>'
        f'<div class="value pos">{html.escape(best_str)}</div></div>'
        f'<div class="stat-card"><div class="label">Worst Trade</div>'
        f'<div class="value neg">{html.escape(worst_str)}</div></div>'
        '</div>'
    )
```

- [ ] **Step 4: Add stats bar CSS to the `<style>` block in `index()`**

Inside the `<style>` tag in the HTML template, add after the `.filters button.active` rule:

```css
  .stats-bar {{ display: flex; gap: 16px; margin: 20px 0 32px; flex-wrap: wrap; }}
  .stat-card {{ flex: 1; min-width: 140px; background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px 16px; }}
  .stat-card .label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-card .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .stat-card .value.pos {{ color: #1a7f37; }}
  .stat-card .value.neg {{ color: #c0392b; }}
```

- [ ] **Step 5: Insert the stats bar into the HTML template**

In `index()`, generate the stats bar and insert it between `<h1>` and `{chart_divs}`:

```python
stats_bar = _render_stats_bar(stats)
```

Then in the HTML f-string, change:

```html
<h1>Trading Analytics</h1>
{chart_divs}
```

to:

```html
<h1>Trading Analytics</h1>
{stats_bar}
{chart_divs}
```

- [ ] **Step 6: Verify manually**

```
python -m analytics.server
```

Open `http://127.0.0.1:8080` — confirm the five stat cards appear below the heading with correct values.

- [ ] **Step 7: Run full test suite to confirm nothing broke**

```
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```
git add analytics/server.py
git commit -m "feat: add summary stats bar to analytics dashboard"
```

---

### Task 4: Add four new Plotly charts

**Files:**
- Modify: `analytics/server.py` — `_query_charts()`

- [ ] **Step 1: Add win rate by ticker chart**

At the end of `_query_charts`, before the `charts = {` dict, add:

```python
    # Win rate by ticker (≥2 closed trades)
    wr_rows = con.execute(
        "SELECT ticker, "
        "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate, "
        "COUNT(*) AS cnt "
        "FROM trades WHERE closed_at IS NOT NULL AND ticker IS NOT NULL "
        "GROUP BY ticker HAVING cnt >= 2 ORDER BY win_rate DESC"
    ).fetchall()
    fig_wr = go.Figure(go.Bar(
        x=[r["win_rate"] for r in wr_rows],
        y=[r["ticker"] for r in wr_rows],
        orientation="h",
    ))
    fig_wr.update_layout(title="Win Rate by Ticker", xaxis_title="Win Rate %", yaxis_title="Ticker",
                         height=max(200, len(wr_rows) * 30 + 80))
```

- [ ] **Step 2: Add P&L by hour-of-day chart**

```python
    # Avg P&L by hour of day (UTC)
    hour_rows = con.execute(
        "SELECT strftime('%H', closed_at) AS hour, AVG(pnl_usd) AS avg_pnl "
        "FROM trades WHERE pnl_usd IS NOT NULL AND closed_at IS NOT NULL "
        "GROUP BY hour ORDER BY hour"
    ).fetchall()
    fig_hour = go.Figure(go.Bar(
        x=[r["hour"] for r in hour_rows],
        y=[r["avg_pnl"] for r in hour_rows],
    ))
    fig_hour.update_layout(title="Avg P&L by Hour of Day (UTC)", xaxis_title="Hour", yaxis_title="Avg USD")
```

- [ ] **Step 3: Add confidence vs. outcome chart**

```python
    # Confidence vs outcome
    conf_rows = con.execute(
        "SELECT d.confidence, t.pnl_pct, t.ticker "
        "FROM trades t JOIN llm_decisions d ON d.id = t.decision_id "
        "WHERE t.pnl_pct IS NOT NULL AND d.confidence IS NOT NULL"
    ).fetchall()
    fig_conf = go.Figure(go.Scatter(
        x=[r["confidence"] for r in conf_rows],
        y=[r["pnl_pct"] * 100 for r in conf_rows],
        mode="markers",
        text=[r["ticker"] for r in conf_rows],
    ))
    fig_conf.update_layout(title="Confidence vs Outcome", xaxis_title="LLM Confidence", yaxis_title="P&L %")
```

- [ ] **Step 4: Add fill latency trend chart**

```python
    # Fill latency trend
    lat_rows = con.execute(
        "SELECT opened_at, fill_latency_sec FROM trades "
        "WHERE fill_latency_sec IS NOT NULL ORDER BY opened_at"
    ).fetchall()
    fig_lat = go.Figure(go.Scatter(
        x=[r["opened_at"] for r in lat_rows],
        y=[r["fill_latency_sec"] for r in lat_rows],
        mode="lines+markers",
    ))
    fig_lat.update_layout(title="Fill Latency Trend", xaxis_title="Time", yaxis_title="Seconds")
```

- [ ] **Step 5: Register the four new figures in the `charts` dict**

The existing `charts` dict ends with `"actions": _fig_json(fig_actions)`. Add four new entries:

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
    }
```

- [ ] **Step 6: Verify manually**

```
python -m analytics.server
```

Open `http://127.0.0.1:8080` — confirm four new charts appear below the existing six.

- [ ] **Step 7: Run full test suite**

```
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```
git add analytics/server.py
git commit -m "feat: add win-rate, P&L-by-hour, confidence-vs-outcome, latency-trend charts"
```

---

### Task 5: Add inline drill-down to the trades table

**Files:**
- Modify: `analytics/server.py`

- [ ] **Step 1: Add `decision_id` to the recent trades query**

In `_query_charts`, the recent trades query currently starts with:

```python
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason, t.closed_at "
```

Change it to:

```python
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason, t.closed_at, d.id AS decision_id "
```

- [ ] **Step 2: Add drill-down CSS to the `<style>` block in `index()`**

Append to the CSS inside the `<style>` tag (after the `.stat-card` rules added in Task 3):

```css
  .expand-btn {{ cursor: pointer; color: #888; user-select: none; text-align: center; width: 24px; }}
  .detail-row td {{ background: #f4f7ff; padding: 12px 16px; font-size: 13px; border-top: none; }}
  .detail-row .reasoning {{ white-space: pre-wrap; margin-top: 8px; color: #444; line-height: 1.5; }}
  .detail-row .meta {{ color: #888; font-size: 12px; margin-top: 4px; }}
```

- [ ] **Step 3: Add expand-indicator column to the table header**

In the HTML template, change:

```html
  <tr><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
      <th>P&amp;L USD</th><th>P&amp;L %</th><th>Exit</th></tr>
```

to:

```html
  <tr><th></th><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
      <th>P&amp;L USD</th><th>P&amp;L %</th><th>Exit</th></tr>
```

- [ ] **Step 4: Add `decision_id` and expand button to each table row**

In `index()`, the `table_rows` loop builds each `<tr>`. Change:

```python
        table_rows += (
            f'<tr data-action="{action}" data-closed="{closed}">'
            f"<td>{ts}</td>"
```

to:

```python
        decision_id = r.get("decision_id") or ""
        table_rows += (
            f'<tr class="trade-row" data-action="{action}" data-closed="{closed}" data-decision-id="{decision_id}">'
            f'<td class="expand-btn">▶</td>'
            f"<td>{ts}</td>"
```

- [ ] **Step 5: Replace the existing `<script>` block with the updated version including drill-down**

Replace the entire `<script>` block at the bottom of the HTML template with:

```html
<script>
  let currentAction = 'all';
  let closedOnly = false;

  function filterTrades(filter, btn) {{
    if (filter === 'closed') {{
      closedOnly = !closedOnly;
      btn.classList.toggle('active', closedOnly);
    }} else {{
      document.querySelectorAll('.filters button:not(:last-child)').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentAction = filter;
    }}
    applyFilters();
  }}

  function applyFilters() {{
    document.querySelectorAll('#trades-table tbody tr').forEach(row => {{
      if (row.classList.contains('detail-row')) return;
      const actionMatch = currentAction === 'all' || row.dataset.action === currentAction;
      const closedMatch = !closedOnly || row.dataset.closed === 'true';
      row.style.display = (actionMatch && closedMatch) ? '' : 'none';
    }});
  }}

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
        const conf = d.confidence != null ? d.confidence.toFixed(2) : '—';
        const hold = d.hold_hours != null ? d.hold_hours + 'h' : '—';
        const detail = document.createElement('tr');
        detail.className = 'detail-row';
        detail.innerHTML =
          '<td colspan="8">' +
          '<strong>' + esc(d.headline) + '</strong>' +
          '<div class="meta">' + esc(d.ts) + ' &nbsp;|&nbsp; confidence: ' + conf +
          ' &nbsp;|&nbsp; hold: ' + hold + '</div>' +
          '<div class="reasoning">' + esc(d.reasoning) + '</div>' +
          '</td>';
        row.after(detail);
        row.querySelector('.expand-btn').textContent = '▼';
      }});
  }});
</script>
```

- [ ] **Step 6: Verify manually**

```
python -m analytics.server
```

Open `http://127.0.0.1:8080`:
1. Click any trade row with a decision — confirm it expands with headline, confidence, and reasoning.
2. Click it again — confirm it collapses.
3. Toggle the "Buy" filter — confirm detail rows are not hidden by the filter.
4. Click a row with no `decision_id` — confirm nothing crashes.

- [ ] **Step 7: Run full test suite**

```
python -m pytest tests/ -v
```

Expected: all tests pass.

- [ ] **Step 8: Commit**

```
git add analytics/server.py
git commit -m "feat: add inline drill-down row with LLM reasoning to trades table"
```
