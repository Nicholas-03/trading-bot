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
        "SELECT news_event_id FROM llm_decisions WHERE id = ?", (decision_id,)
    ).fetchone()
    if row is None:
        return None
    news_event_id = row["news_event_id"]

    headline_row = con.execute(
        "SELECT headline, ts FROM news_events WHERE id = ?", (news_event_id,)
    ).fetchone()

    decision_rows = con.execute(
        "SELECT provider, action, ticker, confidence, hold_hours, reasoning, latency_sec, cost_usd "
        "FROM llm_decisions WHERE news_event_id = ? "
        "ORDER BY CASE provider "
        "WHEN 'claude' THEN 0 WHEN 'gemini' THEN 1 WHEN 'deepseek' THEN 2 WHEN 'chatgpt' THEN 3 ELSE 4 END",
        (news_event_id,),
    ).fetchall()

    return {
        "headline": headline_row["headline"] if headline_row else None,
        "ts": headline_row["ts"] if headline_row else None,
        "decisions": [dict(d) for d in decision_rows],
    }


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

    # 7: Win rate by ticker (≥2 closed trades)
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

    # 8: Avg P&L by hour of day (UTC)
    hour_rows = con.execute(
        "SELECT strftime('%H', closed_at) AS hour, AVG(pnl_usd) AS avg_pnl "
        "FROM trades WHERE pnl_usd IS NOT NULL AND closed_at IS NOT NULL "
        "GROUP BY hour ORDER BY hour"
    ).fetchall()
    fig_hour = go.Figure(go.Bar(
        x=[r["hour"] for r in hour_rows],
        y=[r["avg_pnl"] for r in hour_rows],
    ))
    fig_hour.update_layout(
        title="Avg P&L by Hour of Day (UTC)",
        xaxis_title="Hour",
        yaxis_title="Avg USD",
        xaxis=dict(categoryorder="category ascending"),
    )

    # 9: Confidence vs outcome
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

    # 10: Fill latency trend
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

    # Recent news → decision → outcome table
    # In multi-provider mode each news event has 3 llm_decisions rows; show only
    # Claude's row (or legacy NULL-provider row) so one row appears per event.
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason, t.closed_at, d.id AS decision_id "
        "FROM news_events n "
        "JOIN llm_decisions d ON d.news_event_id = n.id "
        "    AND (d.provider = 'claude' OR d.provider IS NULL) "
        "LEFT JOIN trades t ON t.decision_id = d.id "
        "ORDER BY n.ts DESC LIMIT 500"
    ).fetchall()

    # 11: Provider response latency (box plot)
    plat_rows = con.execute(
        "SELECT provider, latency_sec FROM llm_decisions "
        "WHERE provider IS NOT NULL AND latency_sec IS NOT NULL"
    ).fetchall()
    fig_plat = go.Figure()
    for p in ["claude", "gemini", "deepseek", "chatgpt"]:
        vals = [r["latency_sec"] for r in plat_rows if r["provider"] == p]
        if vals:
            fig_plat.add_trace(go.Box(y=vals, name=p))
    fig_plat.update_layout(title="Provider Response Latency", yaxis_title="Seconds")

    # 12: LLM agreement rate (events where all 3 providers agreed on the same action)
    agree_rows = con.execute(
        "SELECT COUNT(DISTINCT action) AS unique_actions "
        "FROM llm_decisions WHERE provider IS NOT NULL "
        "GROUP BY news_event_id HAVING COUNT(*) = 4"
    ).fetchall()
    agreed = sum(1 for r in agree_rows if r["unique_actions"] == 1)
    disagreed = len(agree_rows) - agreed
    fig_agree = go.Figure(go.Bar(x=["Agreed", "Disagreed"], y=[agreed, disagreed]))
    fig_agree.update_layout(
        title="LLM Agreement Rate (4-provider events)", yaxis_title="News Events"
    )

    # 13: Total cost per provider
    cost_rows = con.execute(
        "SELECT provider, SUM(cost_usd) AS total_cost "
        "FROM llm_decisions "
        "WHERE provider IS NOT NULL AND cost_usd IS NOT NULL "
        "GROUP BY provider "
        "ORDER BY CASE provider "
        "WHEN 'claude' THEN 0 WHEN 'gemini' THEN 1 WHEN 'deepseek' THEN 2 WHEN 'chatgpt' THEN 3 ELSE 4 END"
    ).fetchall()
    fig_cost = go.Figure(go.Bar(
        x=[r["provider"] for r in cost_rows],
        y=[r["total_cost"] for r in cost_rows],
    ))
    fig_cost.update_layout(
        title="Total Cost per Provider (USD)",
        xaxis_title="Provider",
        yaxis_title="USD",
    )

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
        "provider_latency": _fig_json(fig_plat),
        "agreement_rate": _fig_json(fig_agree),
        "total_cost": _fig_json(fig_cost),
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
        decision_id = r.get("decision_id") or ""
        table_rows += (
            f'<tr class="trade-row" data-action="{action}" data-closed="{closed}" data-decision-id="{decision_id}">'
            f'<td class="expand-btn">&#9658;</td>'
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
  .expand-btn {{ cursor: pointer; color: #888; user-select: none; text-align: center; width: 24px; }}
  .detail-row td {{ background: #f4f7ff; padding: 12px 16px; font-size: 13px; border-top: none; }}
  .detail-row .reasoning {{ white-space: pre-wrap; margin-top: 8px; color: #444; line-height: 1.5; }}
  .detail-row .meta {{ color: #888; font-size: 12px; margin-top: 4px; }}
  .provider-compare {{ border-collapse: collapse; width: 100%; margin-top: 10px; font-size: 12px; }}
  .provider-compare th, .provider-compare td {{ border: 1px solid #ddd; padding: 5px 8px; text-align: left; vertical-align: top; }}
  .provider-compare th {{ background: #e8e8e8; font-weight: 600; }}
  .provider-compare td:first-child {{ font-weight: 600; white-space: nowrap; }}
  .provider-compare td {{ max-width: 280px; white-space: pre-wrap; word-break: break-word; }}
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
  <tr><th></th><th>Time (UTC)</th><th>Headline</th><th>Action</th><th>Ticker</th>
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
            ['Cost',       decisions.map(p => p.cost_usd != null ? '$' + p.cost_usd.toFixed(6) : '—')],
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
        }} else {{
          providerHtml = '<div class="meta">No decision data available.</div>';
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
