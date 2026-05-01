# analytics/server.py

import html
import json
import os
import sqlite3

import plotly.graph_objects as go
import plotly.utils
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

DB_PATH = os.getenv("ANALYTICS_DB_PATH", "data/trades.db")

app = FastAPI()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fig_json(fig: go.Figure) -> dict:
    return json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig))


def _query_decision(con: sqlite3.Connection, decision_id: int) -> dict | None:
    row = con.execute(
        "SELECT n.headline, n.ts, d.action, d.ticker, d.confidence, d.hold_hours, d.reasoning "
        "FROM llm_decisions d JOIN news_events n ON n.id = d.news_event_id "
        "WHERE d.id = ?",
        (decision_id,),
    ).fetchone()
    return dict(row) if row is not None else None


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


def _build_page_data() -> tuple[dict, dict, list[dict]]:
    con = _conn()
    try:
        charts, recent = _query_charts(con)
        stats = _query_stats(con)
        return charts, stats, recent
    finally:
        con.close()


def _query_charts(con: sqlite3.Connection) -> tuple[dict, list[dict]]:
    # 1 & 2: Cumulative and daily P&L
    rows = con.execute(
        "SELECT date(closed_at) as day, SUM(pnl_usd) as dpnl "
        "FROM trades WHERE pnl_usd IS NOT NULL AND closed_at IS NOT NULL "
        "GROUP BY day ORDER BY day"
    ).fetchall()
    days = [r["day"] for r in rows]
    daily_pnl = [r["dpnl"] for r in rows]
    cumulative: list[float] = []
    total = 0.0
    for p in daily_pnl:
        total += p
        cumulative.append(total)

    fig_cum = go.Figure(go.Scatter(x=days, y=cumulative, mode="lines+markers"))
    fig_cum.update_layout(title="Cumulative P&L", xaxis_title="Date", yaxis_title="USD")

    fig_daily = go.Figure(go.Bar(x=days, y=daily_pnl))
    fig_daily.update_layout(title="Daily P&L", xaxis_title="Date", yaxis_title="USD")

    # 3: Exit reason donut
    exit_rows = con.execute(
        "SELECT exit_reason, COUNT(*) as cnt FROM trades "
        "WHERE exit_reason IS NOT NULL GROUP BY exit_reason"
    ).fetchall()
    fig_exit = go.Figure(go.Pie(
        labels=[r["exit_reason"] for r in exit_rows],
        values=[r["cnt"] for r in exit_rows],
        hole=0.4,
    ))
    fig_exit.update_layout(title="Exit Reason Distribution")

    # 4: P&L % distribution
    pct_rows = con.execute("SELECT pnl_pct FROM trades WHERE pnl_pct IS NOT NULL").fetchall()
    pcts = [r["pnl_pct"] * 100 for r in pct_rows]
    fig_dist = go.Figure(go.Histogram(x=pcts, nbinsx=20))
    fig_dist.update_layout(title="P&L % Distribution at Exit", xaxis_title="P&L %", yaxis_title="Count")

    # 5: Trade duration histogram
    dur_rows = con.execute(
        "SELECT (julianday(closed_at) - julianday(opened_at)) * 24 * 60 AS mins "
        "FROM trades WHERE closed_at IS NOT NULL AND opened_at IS NOT NULL"
    ).fetchall()
    durations = [r["mins"] for r in dur_rows if r["mins"] is not None]
    fig_dur = go.Figure(go.Histogram(x=durations, nbinsx=20))
    fig_dur.update_layout(title="Trade Duration", xaxis_title="Minutes held", yaxis_title="Count")

    # 6: LLM decision counts
    action_rows = con.execute(
        "SELECT action, COUNT(*) as cnt FROM llm_decisions GROUP BY action"
    ).fetchall()
    fig_actions = go.Figure(go.Bar(
        x=[r["action"] for r in action_rows],
        y=[r["cnt"] for r in action_rows],
    ))
    fig_actions.update_layout(title="LLM Decision Counts", xaxis_title="Action", yaxis_title="Count")

    # Recent news → decision → outcome table
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason, t.closed_at "
        "FROM news_events n "
        "JOIN llm_decisions d ON d.news_event_id = n.id "
        "LEFT JOIN trades t ON t.decision_id = d.id "
        "ORDER BY n.ts DESC LIMIT 500"
    ).fetchall()

    charts = {
        "cumulative": _fig_json(fig_cum),
        "daily": _fig_json(fig_daily),
        "exit": _fig_json(fig_exit),
        "dist": _fig_json(fig_dist),
        "duration": _fig_json(fig_dur),
        "actions": _fig_json(fig_actions),
    }
    return charts, [dict(r) for r in recent]


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    charts, stats, recent = _build_page_data()

    stats_bar = _render_stats_bar(stats)

    chart_divs = ""
    for key, fig_data in charts.items():
        chart_divs += (
            f'<div id="c-{key}" style="margin-bottom:40px"></div>\n'
            f'<script>Plotly.newPlot("c-{key}",'
            f'{json.dumps(fig_data["data"])},{json.dumps(fig_data["layout"])})</script>\n'
        )

    table_rows = ""
    for r in recent:
        pnl_usd = f"{r['pnl_usd']:+.2f}" if r["pnl_usd"] is not None else "—"
        pnl_pct = f"{r['pnl_pct'] * 100:+.1f}%" if r["pnl_pct"] is not None else "—"
        headline = html.escape((r["headline"] or "")[:60])
        ts = html.escape((r["ts"] or "")[:16])
        action = html.escape(r["action"] or "")
        ticker = html.escape(r["ticker"]) if r["ticker"] else "—"
        exit_reason = html.escape(r["exit_reason"]) if r["exit_reason"] else "—"
        closed = "true" if r["closed_at"] is not None else "false"
        table_rows += (
            f'<tr data-action="{action}" data-closed="{closed}">'
            f"<td>{ts}</td>"
            f"<td>{headline}</td>"
            f"<td>{action}</td>"
            f"<td>{ticker}</td>"
            f"<td>{pnl_usd}</td>"
            f"<td>{pnl_pct}</td>"
            f"<td>{exit_reason}</td>"
            f"</tr>\n"
        )

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Trading Analytics</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>
  body {{ font-family: sans-serif; max-width: 1100px; margin: 40px auto; padding: 0 20px; color: #222; }}
  h1 {{ border-bottom: 2px solid #eee; padding-bottom: 8px; }}
  h2 {{ margin-top: 48px; }}
  table {{ border-collapse: collapse; width: 100%; margin-top: 12px; font-size: 13px; }}
  th, td {{ border: 1px solid #ddd; padding: 7px 10px; text-align: left; }}
  th {{ background: #f6f6f6; font-weight: 600; }}
  tr:nth-child(even) {{ background: #fafafa; }}
  .filters {{ display: flex; gap: 8px; margin-top: 16px; }}
  .filters button {{ padding: 6px 16px; border: 1px solid #ccc; border-radius: 4px; background: #f6f6f6; cursor: pointer; font-size: 13px; }}
  .filters button.active {{ background: #222; color: #fff; border-color: #222; }}
  .stats-bar {{ display: flex; gap: 16px; margin: 20px 0 32px; flex-wrap: wrap; }}
  .stat-card {{ flex: 1; min-width: 140px; background: #f6f6f6; border: 1px solid #e0e0e0; border-radius: 6px; padding: 12px 16px; }}
  .stat-card .label {{ font-size: 11px; color: #888; text-transform: uppercase; letter-spacing: 0.5px; }}
  .stat-card .value {{ font-size: 22px; font-weight: 700; margin-top: 4px; }}
  .stat-card .value.pos {{ color: #1a7f37; }}
  .stat-card .value.neg {{ color: #c0392b; }}
</style>
</head>
<body>
<h1>Trading Analytics</h1>
{stats_bar}
{chart_divs}
<h2>Recent Trades (last 500)</h2>
<div class="filters">
  <button class="active" onclick="filterTrades('all', this)">All</button>
  <button onclick="filterTrades('buy', this)">Buy</button>
  <button onclick="filterTrades('short', this)">Short</button>
  <button onclick="filterTrades('hold', this)">Hold</button>
  <button onclick="filterTrades('closed', this)" style="margin-left:16px">Closed only</button>
</div>
<table id="trades-table">
<thead>
  <tr><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
      <th>P&amp;L USD</th><th>P&amp;L %</th><th>Exit</th></tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
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
      const actionMatch = currentAction === 'all' || row.dataset.action === currentAction;
      const closedMatch = !closedOnly || row.dataset.closed === 'true';
      row.style.display = (actionMatch && closedMatch) ? '' : 'none';
    }});
  }}
</script>
</body>
</html>"""
    return HTMLResponse(content=content)


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


if __name__ == "__main__":
    uvicorn.run("analytics.server:app", host="127.0.0.1", port=8080, reload=False)
