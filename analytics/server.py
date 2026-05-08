# analytics/server.py

import html
import json
import os
import sqlite3
from datetime import datetime
from zoneinfo import ZoneInfo

import plotly.graph_objects as go
import plotly.utils
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse
from analytics.db import TradeDB

DB_PATH = os.getenv("ANALYTICS_DB_PATH", "data/trades.db")

app = FastAPI()


def _ensure_schema() -> None:
    db = TradeDB(DB_PATH)
    db.close()

    c = sqlite3.connect(DB_PATH)
    for ddl in [
        "ALTER TABLE llm_decisions ADD COLUMN provider TEXT",
        "ALTER TABLE llm_decisions ADD COLUMN latency_sec REAL",
        "ALTER TABLE llm_decisions ADD COLUMN cost_usd REAL",
    ]:
        try:
            c.execute(ddl)
            c.commit()
        except sqlite3.OperationalError:
            pass
    c.close()


_ensure_schema()

# ── Plotly theme ──────────────────────────────────────────────────────────────
_PLOTLY_LAYOUT = dict(
    template="plotly_white",
    font=dict(family="Inter, system-ui, sans-serif", size=12, color="#374151"),
    paper_bgcolor="#ffffff",
    plot_bgcolor="#ffffff",
    margin=dict(l=48, r=24, t=48, b=40),
    title=dict(font=dict(size=14, color="#111827", weight="bold")),
    xaxis=dict(gridcolor="#f3f4f6", linecolor="#e5e7eb"),
    yaxis=dict(gridcolor="#f3f4f6", linecolor="#e5e7eb"),
)
_COLOR_POS  = "#16a34a"
_COLOR_NEG  = "#dc2626"
_COLOR_MAIN = "#2563eb"
_COLOR_BAR  = "#60a5fa"

# ── HTML / CSS ─────────────────────────────────────────────────────────────────
_CSS = """
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');

*, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

:root {
  --bg:      #f6f7f9;
  --surface: #ffffff;
  --border:  #e5e7eb;
  --text:    #111827;
  --muted:   #6b7280;
  --pos:     #16a34a;
  --neg:     #dc2626;
  --accent:  #2563eb;
  --accent-soft: #eff6ff;
  --ink-soft: #475569;
  --radius:  8px;
  --shadow:  0 12px 28px rgba(15,23,42,.06), 0 2px 6px rgba(15,23,42,.05);
}

body {
  font-family: Inter, system-ui, sans-serif;
  background: var(--bg);
  color: var(--text);
  font-size: 14px;
  line-height: 1.5;
}

.page {
  max-width: 1280px;
  margin: 0 auto;
  padding: 32px 24px 64px;
}

/* ── Header ── */
.page-header {
  display: flex;
  align-items: baseline;
  gap: 12px;
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--border);
}
.page-header h1 {
  font-size: 26px;
  font-weight: 700;
  color: var(--text);
}
.page-header .subtitle {
  font-size: 13px;
  color: var(--muted);
  font-weight: 500;
}

/* ── Section headings ── */
.section-title {
  font-size: 13px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .6px;
  margin: 36px 0 14px;
}

/* ── Stats bar ── */
.stats-bar {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
  gap: 12px;
  margin-bottom: 8px;
}
.stat-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  padding: 16px 18px;
  box-shadow: var(--shadow);
  min-height: 96px;
}
.stat-card .label {
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .5px;
  margin-bottom: 6px;
}
.stat-card .value {
  font-size: 25px;
  font-weight: 700;
  color: var(--text);
  line-height: 1.15;
}
.stat-card .value.pos { color: var(--pos); }
.stat-card .value.neg { color: var(--neg); }

/* ── Charts grid ── */
.charts-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
  margin-bottom: 8px;
}
.chart-card {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  padding: 16px;
  min-height: 260px;
}
.chart-card.full-width {
  grid-column: 1 / -1;
}

/* ── Filters ── */
.filters {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-bottom: 12px;
}
.filter-group {
  display: flex;
  gap: 4px;
}
.filter-group + .filter-group {
  margin-left: 12px;
  padding-left: 12px;
  border-left: 1px solid var(--border);
}
.filters button {
  padding: 5px 14px;
  border: 1px solid var(--border);
  border-radius: 20px;
  background: var(--surface);
  color: var(--muted);
  cursor: pointer;
  font-size: 12px;
  font-weight: 500;
  font-family: inherit;
  transition: all .15s;
}
.filters button:hover { border-color: var(--accent); color: var(--accent); }
.filters button.active {
  background: var(--accent);
  color: #fff;
  border-color: var(--accent);
  box-shadow: 0 6px 14px rgba(37,99,235,.18);
}

/* ── Table ── */
.table-wrap {
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  box-shadow: var(--shadow);
  overflow-x: auto;
  overflow-y: hidden;
}
table {
  border-collapse: collapse;
  width: 100%;
  min-width: 940px;
  font-size: 13px;
}
thead th {
  background: #f3f4f6;
  padding: 10px 14px;
  text-align: left;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .5px;
  border-bottom: 1px solid var(--border);
  white-space: nowrap;
}
tbody tr.trade-row {
  border-bottom: 1px solid #f3f4f6;
  transition: background .1s;
}
tbody tr.trade-row:hover { background: #f9fafb; }
tbody tr.trade-row[style*="display: none"] + tr.detail-row { display: none; }
tbody tr.trade-row:last-child { border-bottom: none; }
tbody td {
  padding: 10px 14px;
  vertical-align: middle;
  color: var(--text);
}
td.col-time { color: var(--muted); font-size: 12px; white-space: nowrap; }
td.col-headline { max-width: 340px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
td.col-pnl-pos { color: var(--pos); font-weight: 600; }
td.col-pnl-neg { color: var(--neg); font-weight: 600; }
td.col-pnl-nil { color: var(--muted); }

/* Badge */
.badge {
  display: inline-block;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
  letter-spacing: .2px;
  text-transform: lowercase;
}
.badge-buy   { background: #dbeafe; color: #1d4ed8; }
.badge-sell  { background: #fce7f3; color: #be185d; }
.badge-short { background: #fef3c7; color: #b45309; }
.badge-hold  { background: #f3f4f6; color: #6b7280; }

.badge-exit-sl    { background: #fee2e2; color: #b91c1c; }
.badge-exit-tp    { background: #dcfce7; color: #15803d; }
.badge-exit-other { background: #f3f4f6; color: #6b7280; }

/* Expand button */
.expand-btn {
  cursor: pointer;
  color: var(--muted);
  user-select: none;
  text-align: center;
  width: 28px;
  font-size: 10px;
  transition: color .15s;
}
.expand-btn:hover { color: var(--accent); }

/* ── Detail row ── */
tr.detail-row td {
  padding: 16px 20px 20px;
  background: #f8faff;
  border-bottom: 1px solid var(--border);
}
.detail-headline {
  font-size: 13px;
  font-weight: 600;
  color: var(--text);
  margin-bottom: 4px;
}
.detail-ts {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 12px;
}
.detail-reasoning {
  font-size: 12px;
  color: #374151;
  line-height: 1.6;
  white-space: pre-wrap;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 6px;
  padding: 10px 14px;
  margin-top: 6px;
}
.detail-meta {
  font-size: 11px;
  color: var(--muted);
  margin-bottom: 4px;
  display: flex;
  gap: 16px;
}

/* Provider comparison table */
.provider-compare {
  border-collapse: collapse;
  width: 100%;
  margin-top: 8px;
  font-size: 12px;
  border-radius: 6px;
  overflow: hidden;
  border: 1px solid var(--border);
}
.provider-compare thead th {
  background: #f3f4f6;
  padding: 8px 12px;
  font-size: 11px;
  font-weight: 600;
  color: var(--muted);
  text-transform: uppercase;
  letter-spacing: .4px;
  border-bottom: 1px solid var(--border);
  text-align: left;
}
.provider-compare tbody td {
  padding: 8px 12px;
  border-bottom: 1px solid #f3f4f6;
  vertical-align: top;
  color: var(--text);
}
.provider-compare tbody tr:last-child td { border-bottom: none; }
.provider-compare td:first-child {
  font-weight: 600;
  color: var(--muted);
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: .3px;
  white-space: nowrap;
  background: #f9fafb;
}
.provider-compare td.reasoning-cell {
  max-width: 260px;
  white-space: pre-wrap;
  word-break: break-word;
  font-size: 11px;
  color: #374151;
  line-height: 1.5;
}

@media (max-width: 720px) {
  .page { padding: 24px 14px 48px; }
  .page-header {
    display: block;
    margin-bottom: 24px;
  }
  .page-header .subtitle {
    display: block;
    margin-top: 4px;
  }
  .stats-bar { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  .charts-grid { grid-template-columns: 1fr; }
  .chart-card { padding: 12px; }
  .filter-group + .filter-group {
    margin-left: 0;
    padding-left: 0;
    border-left: 0;
  }
}
"""

# ── Helpers ───────────────────────────────────────────────────────────────────

def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=30.0)
    c.execute("PRAGMA busy_timeout = 30000")
    c.row_factory = sqlite3.Row
    return c


def _fig_json(fig: go.Figure) -> dict:
    return json.loads(plotly.utils.PlotlyJSONEncoder().encode(fig))


def _apply_theme(fig: go.Figure, height: int = 300) -> go.Figure:
    fig.update_layout(**_PLOTLY_LAYOUT, height=height)
    return fig


def _display_text(value: str | None) -> str:
    return html.unescape(value or "")


def _parse_iso_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _action_badge(action: str) -> str:
    cls = {"buy": "badge-buy", "sell": "badge-sell", "short": "badge-short"}.get(action.lower(), "badge-hold")
    return f'<span class="badge {cls}">{html.escape(action)}</span>'


def _exit_badge(reason: str) -> str:
    if not reason or reason == "—":
        return "—"
    lo = reason.lower()
    cls = "badge-exit-sl" if "stop" in lo or "sl" in lo else ("badge-exit-tp" if "take" in lo or "tp" in lo else "badge-exit-other")
    return f'<span class="badge {cls}">{html.escape(reason)}</span>'


def _pnl_td(value: float | None, fmt_str: str, pct: bool = False) -> str:
    if value is None:
        return '<td class="col-pnl-nil">—</td>'
    v = value * 100 if pct else value
    cls = "col-pnl-pos" if v >= 0 else "col-pnl-neg"
    sign = "+" if v >= 0 else ""
    return f'<td class="{cls}">{sign}{v:.{1 if pct else 2}f}{"%" if pct else ""}</td>'


# ── DB queries ─────────────────────────────────────────────────────────────────

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
        "SELECT provider, action, ticker, confidence, hold_hours, reasoning, latency_sec, cost_usd, "
        "       is_primary, skip_reason "
        "FROM llm_decisions WHERE news_event_id = ? "
        "ORDER BY is_primary DESC, id ASC",
        (news_event_id,),
    ).fetchall()

    return {
        "headline": _display_text(headline_row["headline"]) if headline_row else None,
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
    events = con.execute("SELECT COUNT(*) AS total FROM news_events").fetchone()["total"] or 0
    decisions = con.execute("SELECT COUNT(*) AS total FROM llm_decisions").fetchone()["total"] or 0
    open_trades = con.execute(
        "SELECT COUNT(*) AS total FROM trades WHERE closed_at IS NULL"
    ).fetchone()["total"] or 0
    total_trades = con.execute("SELECT COUNT(*) AS total FROM trades").fetchone()["total"] or 0
    et = ZoneInfo("America/New_York")
    today_et = datetime.now(et).date()
    closed_rows = con.execute(
        "SELECT closed_at FROM trades WHERE closed_at IS NOT NULL AND pnl_usd IS NOT NULL"
    ).fetchall()
    realized_today_et = sum(
        1
        for r in closed_rows
        if (closed_at := _parse_iso_dt(r["closed_at"])) is not None
        and closed_at.astimezone(et).date() == today_et
    )

    return {
        "events": events,
        "decisions": decisions,
        "total_trades": total_trades,
        "total": total,
        "open_trades": open_trades,
        "realized_today_et": realized_today_et,
        "win_rate": win_rate,
        "total_pnl": total_pnl,
        "best": (best["ticker"], best["pnl_usd"]) if best else None,
        "worst": (worst["ticker"], worst["pnl_usd"]) if worst else None,
    }


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

    fig_cum = go.Figure(go.Scatter(
        x=days, y=cumulative, mode="lines+markers",
        line=dict(color=_COLOR_MAIN, width=2),
        marker=dict(size=5, color=_COLOR_MAIN),
        fill="tozeroy", fillcolor="rgba(37,99,235,0.08)",
    ))
    _apply_theme(fig_cum, height=280)
    fig_cum.update_layout(title="Cumulative P&L", xaxis_title="Date", yaxis_title="USD")

    bar_colors = [_COLOR_POS if v >= 0 else _COLOR_NEG for v in daily_pnl]
    fig_daily = go.Figure(go.Bar(x=days, y=daily_pnl, marker_color=bar_colors))
    _apply_theme(fig_daily, height=260)
    fig_daily.update_layout(title="Daily P&L", xaxis_title="Date", yaxis_title="USD")

    # 3: Exit reason donut
    exit_rows = con.execute(
        "SELECT exit_reason, COUNT(*) as cnt FROM trades "
        "WHERE exit_reason IS NOT NULL GROUP BY exit_reason"
    ).fetchall()
    fig_exit = go.Figure(go.Pie(
        labels=[r["exit_reason"] for r in exit_rows],
        values=[r["cnt"] for r in exit_rows],
        hole=0.45,
        marker=dict(colors=["#2563eb", "#16a34a", "#dc2626", "#f59e0b", "#8b5cf6"]),
    ))
    _apply_theme(fig_exit, height=260)
    fig_exit.update_layout(title="Exit Reason Distribution")

    # 4: P&L % distribution
    pct_rows = con.execute("SELECT pnl_pct FROM trades WHERE pnl_pct IS NOT NULL").fetchall()
    pcts = [r["pnl_pct"] * 100 for r in pct_rows]
    fig_dist = go.Figure(go.Histogram(x=pcts, nbinsx=20, marker_color=_COLOR_BAR))
    _apply_theme(fig_dist, height=260)
    fig_dist.update_layout(title="P&L % Distribution at Exit", xaxis_title="P&L %", yaxis_title="Count")

    # 5: Trade duration histogram
    dur_rows = con.execute(
        "SELECT (julianday(closed_at) - julianday(opened_at)) * 24 * 60 AS mins "
        "FROM trades WHERE closed_at IS NOT NULL AND opened_at IS NOT NULL"
    ).fetchall()
    durations = [r["mins"] for r in dur_rows if r["mins"] is not None]
    fig_dur = go.Figure(go.Histogram(x=durations, nbinsx=20, marker_color=_COLOR_BAR))
    _apply_theme(fig_dur, height=260)
    fig_dur.update_layout(title="Trade Duration", xaxis_title="Minutes held", yaxis_title="Count")

    # 6: LLM decision counts
    action_rows = con.execute(
        "SELECT action, COUNT(*) as cnt FROM llm_decisions GROUP BY action"
    ).fetchall()
    fig_actions = go.Figure(go.Bar(
        x=[r["action"] for r in action_rows],
        y=[r["cnt"] for r in action_rows],
        marker_color=_COLOR_MAIN,
    ))
    _apply_theme(fig_actions, height=260)
    fig_actions.update_layout(title="LLM Decision Counts", xaxis_title="Action", yaxis_title="Count")

    # 6b: Decision mix over time
    mix_rows = con.execute(
        "SELECT date(ts) AS day, lower(action) AS action, COUNT(*) AS cnt "
        "FROM llm_decisions WHERE ts IS NOT NULL "
        "GROUP BY day, lower(action) ORDER BY day"
    ).fetchall()
    mix_days = sorted({r["day"] for r in mix_rows})
    mix_actions = ["buy", "short", "sell", "hold"]
    mix_colors = {
        "buy": "#2563eb",
        "short": "#f59e0b",
        "sell": "#be185d",
        "hold": "#94a3b8",
    }
    fig_mix = go.Figure()
    for action in mix_actions:
        values = [
            sum(r["cnt"] for r in mix_rows if r["day"] == day and r["action"] == action)
            for day in mix_days
        ]
        if any(values):
            fig_mix.add_trace(go.Bar(
                x=mix_days,
                y=values,
                name=action,
                marker_color=mix_colors[action],
            ))
    _apply_theme(fig_mix, height=280)
    fig_mix.update_layout(
        title="Decision Mix Over Time",
        xaxis_title="Date",
        yaxis_title="LLM decisions",
        barmode="stack",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1),
    )

    # 7: Win rate by ticker (≥2 closed trades)
    wr_rows = con.execute(
        "SELECT ticker, "
        "SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END) * 100.0 / COUNT(*) AS win_rate, "
        "COUNT(*) AS cnt "
        "FROM trades WHERE closed_at IS NOT NULL AND ticker IS NOT NULL "
        "GROUP BY ticker HAVING cnt >= 2 ORDER BY win_rate DESC"
    ).fetchall()
    wr_colors = [_COLOR_POS if r["win_rate"] >= 50 else _COLOR_NEG for r in wr_rows]
    fig_wr = go.Figure(go.Bar(
        x=[r["win_rate"] for r in wr_rows],
        y=[r["ticker"] for r in wr_rows],
        orientation="h",
        marker_color=wr_colors,
    ))
    chart_h = max(260, len(wr_rows) * 28 + 80)
    _apply_theme(fig_wr, height=chart_h)
    fig_wr.update_layout(title="Win Rate by Ticker", xaxis_title="Win Rate %", yaxis_title="Ticker")

    # 8: Avg P&L by hour
    hour_rows = con.execute(
        "SELECT strftime('%H', closed_at) AS hour, AVG(pnl_usd) AS avg_pnl "
        "FROM trades WHERE pnl_usd IS NOT NULL AND closed_at IS NOT NULL "
        "GROUP BY hour ORDER BY hour"
    ).fetchall()
    hour_colors = [_COLOR_POS if r["avg_pnl"] >= 0 else _COLOR_NEG for r in hour_rows]
    fig_hour = go.Figure(go.Bar(
        x=[r["hour"] for r in hour_rows],
        y=[r["avg_pnl"] for r in hour_rows],
        marker_color=hour_colors,
    ))
    _apply_theme(fig_hour, height=260)
    fig_hour.update_layout(
        title="Avg P&L by Hour of Day (UTC)",
        xaxis_title="Hour (UTC)", yaxis_title="Avg USD",
        xaxis=dict(categoryorder="category ascending"),
    )

    # 9: Confidence vs outcome
    conf_rows = con.execute(
        "SELECT d.confidence, t.pnl_pct, t.ticker "
        "FROM trades t JOIN llm_decisions d ON d.id = t.decision_id "
        "WHERE t.pnl_pct IS NOT NULL AND d.confidence IS NOT NULL"
    ).fetchall()
    point_colors = [_COLOR_POS if r["pnl_pct"] >= 0 else _COLOR_NEG for r in conf_rows]
    fig_conf = go.Figure(go.Scatter(
        x=[r["confidence"] for r in conf_rows],
        y=[r["pnl_pct"] * 100 for r in conf_rows],
        mode="markers",
        text=[r["ticker"] for r in conf_rows],
        marker=dict(color=point_colors, size=7, opacity=0.75),
    ))
    _apply_theme(fig_conf, height=260)
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
        line=dict(color=_COLOR_MAIN, width=1.5),
        marker=dict(size=4),
    ))
    _apply_theme(fig_lat, height=260)
    fig_lat.update_layout(title="Fill Latency Trend", xaxis_title="Time", yaxis_title="Seconds")

    # Recent news → decision → outcome
    recent = con.execute(
        "SELECT n.ts, n.headline, d.action, d.ticker, d.reasoning, "
        "       d.skip_reason, d.provider, d.is_primary, "
        "       t.pnl_usd, t.pnl_pct, t.exit_reason, t.closed_at, d.id AS decision_id "
        "FROM news_events n "
        "JOIN llm_decisions d ON d.news_event_id = n.id "
        "    AND d.id = COALESCE("
        "        (SELECT t2.decision_id FROM trades t2"
         "         JOIN llm_decisions d2 ON d2.id = t2.decision_id"
        "         WHERE d2.news_event_id = n.id ORDER BY t2.opened_at DESC LIMIT 1),"
        "        (SELECT id FROM llm_decisions WHERE news_event_id = n.id AND is_primary = 1 ORDER BY id LIMIT 1),"
        "        (SELECT id FROM llm_decisions WHERE news_event_id = n.id AND skip_reason IS NOT NULL ORDER BY id LIMIT 1),"
        "        (SELECT MIN(id) FROM llm_decisions WHERE news_event_id = n.id)"
        "    ) "
        "LEFT JOIN trades t ON t.decision_id = d.id "
        "ORDER BY n.ts DESC"
    ).fetchall()

    # 11: Provider response latency
    plat_rows = con.execute(
        "SELECT provider, latency_sec FROM llm_decisions "
        "WHERE provider IS NOT NULL AND latency_sec IS NOT NULL"
    ).fetchall()
    fig_plat = go.Figure()
    provider_colors = {"chatgpt": "#16a34a"}
    providers = sorted({r["provider"] for r in plat_rows if r["provider"]})
    for p in providers:
        vals = [r["latency_sec"] for r in plat_rows if r["provider"] == p]
        if vals:
            fig_plat.add_trace(go.Box(y=vals, name=p, marker_color=provider_colors.get(p, _COLOR_MAIN)))
    _apply_theme(fig_plat, height=260)
    fig_plat.update_layout(title="Provider Response Latency", yaxis_title="Seconds")

    # 12: LLM agreement rate
    agree_rows = con.execute(
        "SELECT COUNT(DISTINCT action) AS unique_actions "
        "FROM llm_decisions WHERE provider IS NOT NULL "
        "GROUP BY news_event_id HAVING COUNT(*) >= 2"
    ).fetchall()
    agreed = sum(1 for r in agree_rows if r["unique_actions"] == 1)
    disagreed = len(agree_rows) - agreed
    fig_agree = go.Figure(go.Bar(
        x=["Agreed", "Disagreed"],
        y=[agreed, disagreed],
        marker_color=[_COLOR_POS, _COLOR_NEG],
    ))
    _apply_theme(fig_agree, height=260)
    fig_agree.update_layout(title="LLM Agreement Rate (multi-provider events)", yaxis_title="News Events")

    # 13: Total cost per provider
    cost_rows = con.execute(
        "SELECT provider, SUM(cost_usd) AS total_cost "
        "FROM llm_decisions "
        "WHERE provider IS NOT NULL AND cost_usd IS NOT NULL "
        "GROUP BY provider "
        "ORDER BY CASE provider WHEN 'chatgpt' THEN 0 ELSE 1 END, provider"
    ).fetchall()
    fig_cost = go.Figure(go.Bar(
        x=[r["provider"] for r in cost_rows],
        y=[r["total_cost"] for r in cost_rows],
        marker_color=[provider_colors.get(r["provider"], _COLOR_BAR) for r in cost_rows],
    ))
    _apply_theme(fig_cost, height=260)
    fig_cost.update_layout(title="Total Cost per Provider (USD)", xaxis_title="Provider", yaxis_title="USD")

    charts = {
        "cumulative":       _fig_json(fig_cum),
        "daily":            _fig_json(fig_daily),
        "exit":             _fig_json(fig_exit),
        "dist":             _fig_json(fig_dist),
        "duration":         _fig_json(fig_dur),
        "actions":          _fig_json(fig_actions),
        "decision_mix":     _fig_json(fig_mix),
        "win_rate":         _fig_json(fig_wr),
        "pnl_hour":         _fig_json(fig_hour),
        "conf_outcome":     _fig_json(fig_conf),
        "latency_trend":    _fig_json(fig_lat),
        "provider_latency": _fig_json(fig_plat),
        "agreement_rate":   _fig_json(fig_agree),
        "total_cost":       _fig_json(fig_cost),
    }
    return charts, [dict(r) for r in recent]


def _build_page_data() -> tuple[dict, dict, list[dict]]:
    con = _conn()
    try:
        charts, recent = _query_charts(con)
        stats = _query_stats(con)
        return charts, stats, recent
    finally:
        con.close()


# ── Rendering ──────────────────────────────────────────────────────────────────

def _render_stats_bar(stats: dict) -> str:
    pnl_cls = "pos" if stats["total_pnl"] >= 0 else "neg"
    pnl_sign = "+" if stats["total_pnl"] >= 0 else ""
    best_str = f"{stats['best'][0]} {stats['best'][1]:+.2f}" if stats["best"] else "—"
    worst_str = f"{stats['worst'][0]} {stats['worst'][1]:+.2f}" if stats["worst"] else "—"
    best_cls = "pos" if stats["best"] and stats["best"][1] >= 0 else ("neg" if stats["best"] else "")
    worst_cls = "pos" if stats["worst"] and stats["worst"][1] >= 0 else ("neg" if stats["worst"] else "")
    cards = [
        ("News Events",    f"{stats['events']:,}",                  ""),
        ("LLM Calls",      f"{stats['decisions']:,}",               ""),
        ("Total Trades",   str(stats["total_trades"]),                ""),
        ("Closed Trades",  str(stats["total"]),                       ""),
        ("Open Trades",    str(stats["open_trades"]),                 ""),
        ("Realized Today ET", str(stats["realized_today_et"]),         ""),
        ("Win Rate",       f"{stats['win_rate']:.1f}%",               ""),
        ("Total P&amp;L",  f"{pnl_sign}{stats['total_pnl']:.2f}",     pnl_cls),
        ("Best Trade",     html.escape(best_str),                     best_cls),
        ("Worst Trade",    html.escape(worst_str),                    worst_cls),
    ]
    items = "".join(
        f'<div class="stat-card">'
        f'<div class="label">{label}</div>'
        f'<div class="value {cls}">{value}</div>'
        f'</div>'
        for label, value, cls in cards
    )
    return f'<div class="stats-bar">{items}</div>'


# Charts layout: (key, full_width)
_CHART_LAYOUT: list[tuple[str, bool]] = [
    ("cumulative",       True),
    ("daily",            False),
    ("exit",             False),
    ("dist",             False),
    ("duration",         False),
    ("actions",          False),
    ("decision_mix",     True),
    ("agreement_rate",   False),
    ("provider_latency", False),
    ("total_cost",       False),
    ("win_rate",         True),
    ("pnl_hour",         False),
    ("conf_outcome",     False),
    ("latency_trend",    True),
]


def _render_charts(charts: dict) -> str:
    items = ""
    for key, full in _CHART_LAYOUT:
        if key not in charts:
            continue
        fig = charts[key]
        cls = "chart-card full-width" if full else "chart-card"
        items += (
            f'<div class="{cls}">'
            f'<div id="c-{key}"></div>'
            f'</div>\n'
        )
    scripts = ""
    for key, _ in _CHART_LAYOUT:
        if key not in charts:
            continue
        fig = charts[key]
        scripts += (
            f'<script>Plotly.newPlot("c-{key}",'
            f'{json.dumps(fig["data"])},{json.dumps(fig["layout"])},'
            f'{{responsive:true,displayModeBar:false}})</script>\n'
        )
    return f'<div class="charts-grid">{items}</div>\n{scripts}'


def _render_table_rows(recent: list[dict]) -> str:
    rows = ""
    for r in recent:
        pnl_usd_td = _pnl_td(r["pnl_usd"], ".2f")
        pnl_pct_td = _pnl_td(r["pnl_pct"], ".1f", pct=True)
        headline = html.escape(_display_text(r["headline"])[:72])
        ts = html.escape((r["ts"] or "")[:16])
        action = r["action"] or "hold"
        ticker = html.escape(r["ticker"]) if r["ticker"] else "—"
        exit_reason = r["exit_reason"] or r.get("skip_reason") or ""
        closed = "true" if r["closed_at"] is not None else "false"
        decision_id = r.get("decision_id") or ""
        rows += (
            f'<tr class="trade-row" data-action="{html.escape(action)}" '
            f'data-closed="{closed}" data-decision-id="{decision_id}">'
            f'<td class="expand-btn">&#9658;</td>'
            f'<td class="col-time">{ts}</td>'
            f'<td class="col-headline" title="{headline}">{headline}</td>'
            f'<td>{_action_badge(action)}</td>'
            f'<td><strong>{ticker}</strong></td>'
            f'{pnl_usd_td}'
            f'{pnl_pct_td}'
            f'<td>{_exit_badge(exit_reason)}</td>'
            f'</tr>\n'
        )
    return rows


# ── Routes ─────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    charts, stats, recent = _build_page_data()
    stats_bar = _render_stats_bar(stats)
    charts_html = _render_charts(charts)
    table_rows = _render_table_rows(recent)
    loaded_events = f"{len(recent):,} events loaded"

    content = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trading Analytics</title>
<script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
<style>{_CSS}</style>
</head>
<body>
<div class="page">

  <header class="page-header">
    <h1>Trading Analytics</h1>
    <span class="subtitle">Live · {loaded_events}</span>
  </header>

  <p class="section-title">Overview</p>
  {stats_bar}

  <p class="section-title">Charts</p>
  {charts_html}

  <p class="section-title">Recent Decisions</p>

  <div class="filters">
    <div class="filter-group">
      <button class="active" onclick="filterTrades('all',this)">All</button>
      <button onclick="filterTrades('buy',this)">Buy</button>
      <button onclick="filterTrades('short',this)">Short</button>
      <button onclick="filterTrades('hold',this)">Hold</button>
    </div>
    <div class="filter-group">
      <button id="btn-closed" onclick="filterTrades('closed',this)">Closed only</button>
    </div>
  </div>

  <div class="table-wrap">
    <table id="trades-table">
      <thead>
        <tr>
          <th></th>
          <th>Time (UTC)</th>
          <th>Headline</th>
          <th>Action</th>
          <th>Ticker</th>
          <th>P&amp;L USD</th>
          <th>P&amp;L %</th>
          <th>Exit</th>
        </tr>
      </thead>
      <tbody>
{table_rows}
      </tbody>
    </table>
  </div>

</div><!-- .page -->

<script>
  let currentAction = 'all';
  let closedOnly = false;

  function filterTrades(filter, btn) {{
    if (filter === 'closed') {{
      closedOnly = !closedOnly;
      btn.classList.toggle('active', closedOnly);
    }} else {{
      document.querySelectorAll('.filter-group:first-child button').forEach(b => b.classList.remove('active'));
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
      row.querySelector('.expand-btn').innerHTML = '&#9658;';
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
          const fmtConf = v => v != null ? v.toFixed(2) : '—';
          const fmtHold = v => v ? v + 'h' : '—';
          const fmtLat  = v => v != null ? v.toFixed(2) + 's' : '—';
          const fmtCost = v => v != null ? '$' + v.toFixed(6) : '—';
          const fieldRows = [
            ['Action',     decisions.map(p => esc(p.action || '—'))],
            ['Ticker',     decisions.map(p => esc(p.ticker || '—'))],
            ['Confidence', decisions.map(p => fmtConf(p.confidence))],
            ['Hold',       decisions.map(p => fmtHold(p.hold_hours))],
            ['Latency',    decisions.map(p => fmtLat(p.latency_sec))],
            ['Cost',       decisions.map(p => fmtCost(p.cost_usd))],
            ['Reasoning',  decisions.map(p => esc(p.reasoning || ''))],
          ];
          const bodyRows = fieldRows.map(([label, cells]) => {{
            const cls = label === 'Reasoning' ? ' class="reasoning-cell"' : '';
            return '<tr><td>' + label + '</td>' + cells.map(c => '<td' + cls + '>' + c + '</td>').join('') + '</tr>';
          }}).join('');
          providerHtml = '<table class="provider-compare"><thead><tr><th></th>' + heads + '</tr></thead><tbody>' + bodyRows + '</tbody></table>';
        }} else if (decisions.length === 1) {{
          const dec = decisions[0];
          const conf = dec.confidence != null ? dec.confidence.toFixed(2) : '—';
          const hold = dec.hold_hours ? dec.hold_hours + 'h' : '—';
          providerHtml =
            '<div class="detail-meta">' +
            '<span>Confidence: <strong>' + conf + '</strong></span>' +
            '<span>Hold: <strong>' + hold + '</strong></span>' +
            '</div>' +
            '<div class="detail-reasoning">' + esc(dec.reasoning) + '</div>';
        }} else {{
          providerHtml = '<div class="detail-meta">No decision data available.</div>';
        }}

        const detail = document.createElement('tr');
        detail.className = 'detail-row';
        detail.innerHTML =
          '<td colspan="8">' +
          '<div class="detail-headline">' + esc(d.headline) + '</div>' +
          '<div class="detail-ts">' + esc(d.ts) + '</div>' +
          providerHtml +
          '</td>';
        row.after(detail);
        row.querySelector('.expand-btn').innerHTML = '&#9660;';
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
    uvicorn.run(
        "analytics.server:app",
        host=os.getenv("HOST", "localhost"),
        port=int(os.getenv("PORT", "8080")),
        reload=False,
    )
