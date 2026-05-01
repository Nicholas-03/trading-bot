# Analytics Server — Feature Additions Design

**Date:** 2026-05-01
**Status:** Approved

## Overview

Add three groups of improvements to `analytics/server.py`:

1. **Summary stats bar** — key metrics rendered above the charts
2. **Four new Plotly charts** — win rate by ticker, P&L by hour-of-day, confidence vs. outcome, fill latency trend
3. **Inline drill-down** — click a trade row to expand full LLM reasoning + news headline in place

The existing server-side rendering pattern is preserved for features 1 and 2. Feature 3 adds one lightweight JSON endpoint fetched on demand.

---

## 1. Summary Stats Bar

**Location:** Rendered between `<h1>` and the first chart div.

**Data source:** New `_query_stats(con: sqlite3.Connection) -> dict` function. Runs SQL against the existing `trades` table. Called alongside `_query_charts()` in `_build_page_data()` (rename of current `_build_charts()`).

**Stats computed:**

| Stat | SQL |
|---|---|
| Total closed trades | `COUNT(*) WHERE closed_at IS NOT NULL` |
| Win rate | `SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*)` on closed trades |
| Total P&L (USD) | `SUM(pnl_usd)` on closed trades |
| Best trade | `MAX(pnl_usd)` + ticker |
| Worst trade | `MIN(pnl_usd)` + ticker |

**Rendering:** Five `<div class="stat-card">` elements in a flex `<div class="stats-bar">`. Color-coded: positive total P&L green, negative red. No new endpoint — embedded directly in the HTML response.

---

## 2. New Charts

All four added to `_query_charts(con)`. Appended to the `charts` dict under new keys. Rendered in the existing `chart_divs` loop — no template changes needed beyond the new keys appearing.

### 2a. Win Rate by Ticker
- **Type:** Horizontal bar
- **Query:** Per ticker (closed trades only, ≥2 trades), compute `wins / total * 100`
- **Dict key:** `"win_rate"`

### 2b. P&L by Hour-of-Day
- **Type:** Bar
- **Query:** `strftime('%H', closed_at)` grouped, `AVG(pnl_usd)`
- **Dict key:** `"pnl_hour"`

### 2c. Confidence vs. Outcome
- **Type:** Scatter
- **Query:** `JOIN trades t ON t.decision_id = d.id` — x=`d.confidence`, y=`t.pnl_pct * 100`, text=`t.ticker`
- **Dict key:** `"conf_outcome"`

### 2d. Fill Latency Trend
- **Type:** Scatter + lines
- **Query:** `fill_latency_sec` ordered by `opened_at` (non-null only)
- **Dict key:** `"latency_trend"`

---

## 3. Inline Drill-Down

### New endpoint

```
GET /api/decision/{decision_id}
```

Returns JSON:
```json
{
  "headline": "...",
  "ts": "2026-04-30T14:23:00",
  "action": "buy",
  "ticker": "CORT",
  "confidence": 0.82,
  "hold_hours": 2,
  "reasoning": "..."
}
```

Query: `SELECT n.headline, n.ts, d.action, d.ticker, d.confidence, d.hold_hours, d.reasoning FROM llm_decisions d JOIN news_events n ON n.id = d.news_event_id WHERE d.id = ?`

Returns 404 JSON `{"error": "not found"}` if the decision_id does not exist.

### Table changes

- Add a leading `<th></th>` expand-indicator column to the trades table header.
- Each `<tr>` gains `data-decision-id="{decision_id}"` and a leading `<td class="expand-btn">▶</td>`.
- The recent trades query gains `d.id AS decision_id`.

### JS behavior

On click of any `<tr>` (delegate from `tbody`):

1. If a `.detail-row` already follows it, remove it and flip indicator to `▶`. Done.
2. Otherwise: `fetch('/api/decision/{decision_id}')`, insert a new `<tr class="detail-row"><td colspan="8">...</td></tr>` immediately after the clicked row, flip indicator to `▼`.
3. The detail cell renders: headline (bold), timestamp, confidence badge, hold_hours, and reasoning (pre-wrap).
4. Active filters (`applyFilters()`) skip `.detail-row` elements.

---

## Architecture Impact

- `_build_charts()` renamed to `_build_page_data()`, returns `(charts, stats, recent)`.
- `_query_stats()` added as a pure function taking a connection.
- `GET /api/decision/{id}` added as a FastAPI route.
- No new dependencies — Plotly, FastAPI, SQLite already present.
- No schema changes.

---

## Testing

Existing tests in `tests/test_tradier_client.py` etc. are unaffected (pure parsing helpers). The new code has no pure logic to unit-test — validation is done manually by running `python -m analytics.server` and checking the browser.
