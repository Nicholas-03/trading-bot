# analytics/server.py

import json
import os
import sqlite3

import plotly.graph_objects as go
import plotly.utils
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse

DB_PATH = os.getenv("ANALYTICS_DB_PATH", "data/trades.db")

app = FastAPI()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    return c


def _fig_json(fig: go.Figure) -> dict:
    return json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig))


def _build_charts() -> tuple[dict, list[dict]]:
    con = _conn()
    try:
        return _query_charts(con)
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
        "       t.pnl_usd, t.pnl_pct, t.exit_reason "
        "FROM news_events n "
        "JOIN llm_decisions d ON d.news_event_id = n.id "
        "LEFT JOIN trades t ON t.decision_id = d.id "
        "ORDER BY n.ts DESC LIMIT 20"
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
    charts, recent = _build_charts()

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
        headline = (r["headline"] or "")[:60]
        table_rows += (
            f"<tr>"
            f"<td>{(r['ts'] or '')[:16]}</td>"
            f"<td>{headline}</td>"
            f"<td>{r['action']}</td>"
            f"<td>{r['ticker'] or '—'}</td>"
            f"<td>{pnl_usd}</td>"
            f"<td>{pnl_pct}</td>"
            f"<td>{r['exit_reason'] or '—'}</td>"
            f"</tr>\n"
        )

    html = f"""<!DOCTYPE html>
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
</style>
</head>
<body>
<h1>Trading Analytics</h1>
{chart_divs}
<h2>Recent Trades (last 20)</h2>
<table>
<thead>
  <tr><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
      <th>P&amp;L USD</th><th>P&amp;L %</th><th>Exit</th></tr>
</thead>
<tbody>
{table_rows}
</tbody>
</table>
</body>
</html>"""
    return HTMLResponse(content=html)


if __name__ == "__main__":
    uvicorn.run("analytics.server:app", host="0.0.0.0", port=8080, reload=False)
