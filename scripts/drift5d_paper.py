"""Forward paper test of the frozen 5-day post-news drift rule (results/frozen_rule_drift5d.json) on Alpaca PAPER.

    .venv/bin/python scripts/drift5d_paper.py day      # once per trading day, between 15:45 and 15:49 ET
    .venv/bin/python scripts/drift5d_paper.py daemon   # waits for 15:46 ET every trading day and runs `day`
    .venv/bin/python scripts/drift5d_paper.py report   # realised and open P&L of the paper trades

`day`: covers (buy at the close) positions entered 5 trading days ago; then scans today's news (09:31-15:30 ET) with
the exact label code used in the backtest (scripts/build_alpaca_labels.py, bars up to now) and, for each stock whose
first hour after news fell >= 10% versus SPY (first such news per stock today, price >= $2), sells short
NOTIONAL_USD at the close (market-on-close) if Alpaca lets it be shorted. Every qualifying stock is logged, shortable
or not, to results/drift5d_paper.jsonl, because borrow availability is the main unknown of this edge.
Refuses to run against a live (non-paper) Alpaca account.
"""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

LOG = ROOT / "results" / "drift5d_paper.jsonl"
THRESHOLD = -0.10
HOLD_DAYS = 5
NOTIONAL_USD = 2000.0
MAX_OPEN = 15
MIN_PRICE = 2.0


def trading(method: str, path: str, body: dict | None = None, params: dict | None = None):
    # the dedicated paper account (ALPACA_PAPER2_*) if configured, else the bot's Alpaca account
    pre = "ALPACA_PAPER2_" if os.environ.get("ALPACA_PAPER2_API_KEY") else "ALPACA_"
    base = os.environ[pre + "BASE_URL"].rstrip("/")
    if "paper" not in base:
        sys.exit("ALPACA_BASE_URL is not a paper account; this script only paper-trades")
    base = base if base.endswith("/v2") else base + "/v2"
    url = base + path + ("?" + bal.urllib.parse.urlencode(params) if params else "")
    req = urllib.request.Request(url, method=method, data=json.dumps(body).encode() if body else None,
                                 headers={"APCA-API-KEY-ID": os.environ[pre + "API_KEY"],
                                          "APCA-API-SECRET-KEY": os.environ[pre + "SECRET_KEY"],
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        return {"error": e.code, "message": e.read().decode()[:300]}


def log(event: dict) -> None:
    LOG.parent.mkdir(exist_ok=True)
    with open(LOG, "a") as f:
        f.write(json.dumps({"logged": datetime.now(timezone.utc).isoformat(), **event}) + "\n")


def read_log() -> list[dict]:
    return [json.loads(x) for x in open(LOG)] if LOG.exists() else []


def trading_days(start: date, end: date) -> list[str]:
    cal = trading("GET", "/calendar", params={"start": start.isoformat(), "end": end.isoformat()})
    return [d["date"] for d in cal] if isinstance(cal, list) else []


def exit_date(entry_day: str) -> str | None:
    d0 = date.fromisoformat(entry_day)
    days = [d for d in trading_days(d0, d0 + timedelta(days=15)) if d > entry_day]
    return days[HOLD_DAYS - 1] if len(days) >= HOLD_DAYS else None


def signals(day: date) -> list[dict]:
    """Qualifying (news, stock) pairs today, computed exactly as in the backtest labels, with bars up to now."""
    news = bal.fetch_news(day)
    symbols = sorted({s for n in news for s in n["symbols"] if s.isascii() and s.replace(".", "").isalpha()} | {"SPY"})
    bars = bal.fetch_bars(day, symbols) if news else {}
    spy = bars.get("SPY", {})
    flatten = int(datetime(day.year, day.month, day.day, 15, 50, tzinfo=bal.ET).timestamp())
    best: dict[str, dict] = {}
    for n in sorted(news, key=lambda n: n["created_at"]):
        ts = int(datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp())
        for sym in dict.fromkeys(n["symbols"]):
            if sym in best or not spy:
                continue
            lab = bal.label_pair(ts, bars.get(sym, {}), spy, flatten)
            if lab and lab["excess_1h"] <= THRESHOLD:
                best[sym] = {"ticker": sym, "news_id": n["id"], "news_ts": n["created_at"], "headline": n["headline"],
                             "first_hour_excess": lab["excess_1h"], "entry_1m": lab["entry"], "tradable_gate": lab["tradable"],
                             "last": bars[sym][max(bars[sym])][3]}
    return list(best.values())


def day(today: date | None = None) -> None:
    today = today or datetime.now(bal.ET).date()
    if today.isoformat() not in trading_days(today, today):
        print(today, "is not a trading day")
        return
    # 1. cover positions whose 5 trading days are up
    positions = {p["symbol"]: p for p in (trading("GET", "/positions") or []) if isinstance(p, dict)}
    for e in read_log():
        if e.get("event") == "short" and e.get("exit_day") and e["exit_day"] <= today.isoformat() and e["ticker"] in positions:
            p = positions.pop(e["ticker"])
            qty = abs(int(float(p["qty"])))
            resp = trading("POST", "/orders", {"symbol": e["ticker"], "qty": str(qty), "side": "buy", "type": "market",
                                               "time_in_force": "cls"})
            log({"event": "cover", "ticker": e["ticker"], "qty": qty, "entry_day": e["day"], "order": resp})
            print("cover", e["ticker"], qty, resp.get("status", resp))
    # 2. today's signals (virtual shorts when the paper account cannot short: cash account or equity < $2,000)
    acct = trading("GET", "/account")
    virtual = not acct.get("shorting_enabled")
    open_days = {e["ticker"] for e in read_log() if e.get("event") == "short" and (e.get("exit_day") or "") > today.isoformat()}
    held = open_days if virtual else {e["ticker"] for e in read_log() if e.get("event") == "short" and e["ticker"] in positions}
    for s in signals(today):
        ev = {"event": "signal", "day": today.isoformat(), **s}
        asset = trading("GET", f"/assets/{s['ticker']}")
        ev.update(shortable=asset.get("shortable"), easy_to_borrow=asset.get("easy_to_borrow"))
        if s["last"] < MIN_PRICE:
            ev["skip"] = "price"
        elif s["ticker"] in held:
            ev["skip"] = "already short"
        elif len(held) >= MAX_OPEN:
            ev["skip"] = "max open"
        elif not (asset.get("shortable") and asset.get("easy_to_borrow")):
            ev["skip"] = "not shortable at Alpaca"
        log(ev)
        print(f"signal {s['ticker']:6s} first hour {s['first_hour_excess'] * 100:+.1f}% last ${s['last']:.2f} "
              f"shortable={asset.get('shortable')} etb={asset.get('easy_to_borrow')} {ev.get('skip', 'SHORT')} | {s['headline'][:70]}")
        if "skip" in ev:
            continue
        qty = int(NOTIONAL_USD // s["last"])
        if qty < 1:
            continue
        resp = None if virtual else trading("POST", "/orders", {"symbol": s["ticker"], "qty": str(qty), "side": "sell",
                                                                "type": "market", "time_in_force": "cls"})
        log({"event": "short", "virtual": virtual, "day": today.isoformat(), "exit_day": exit_date(today.isoformat()),
             "ticker": s["ticker"], "qty": qty, "ref_price": s["last"], "order": resp})
        held.add(s["ticker"])


def daemon() -> None:
    while True:
        now = datetime.now(bal.ET)
        run_at = now.replace(hour=15, minute=46, second=0, microsecond=0)
        if now > run_at:
            run_at += timedelta(days=1)
        time.sleep((run_at - now).total_seconds())
        try:
            day()
        except Exception as exc:  # keep the daemon alive; the next day retries covers too
            log({"event": "error", "error": repr(exc)})
            print("error:", exc, flush=True)


def report() -> None:
    evs = read_log()
    shorts = [e for e in evs if e.get("event") == "short"]
    sig = [e for e in evs if e.get("event") == "signal"]
    print(f"{len(sig)} signals, {sum('skip' not in e for e in sig)} traded; skips: "
          + ", ".join(f"{k} {sum(e.get('skip') == k for e in sig)}" for k in sorted({e.get('skip') for e in sig if e.get('skip')})))
    pnl = []
    for e in shorts:  # close of the entry day -> close of the exit day (or latest), minus SPY's move
        end = min(e["exit_day"] or e["day"], datetime.now(bal.ET).date().isoformat())
        d = bal.get("/v2/stocks/bars", {"symbols": f"{e['ticker']},SPY", "timeframe": "1Day", "start": e["day"],
                                         "end": end, "feed": "sip", "adjustment": "all"}).get("bars", {})
        c, spy = [b["c"] for b in d.get(e["ticker"], [])], [b["c"] for b in d.get("SPY", [])]
        if len(c) < 2 or len(spy) < 2:
            print(f"{e['day']} {e['ticker']:6s} {'virtual ' if e.get('virtual') else ''}short: no closes yet")
            continue
        r = -((c[-1] / c[0] - 1) - (spy[-1] / spy[0] - 1)) * 100
        done = end >= (e["exit_day"] or "9")
        pnl.append(r) if done else None
        print(f"{e['day']} {e['ticker']:6s} {'virtual ' if e.get('virtual') else ''}short @ {c[0]:.2f} -> {c[-1]:.2f}: "
              f"{r:+.1f}% vs SPY {'(closed)' if done else '(open)'}")
    if pnl:
        print(f"closed: n={len(pnl)} mean {sum(pnl) / len(pnl):+.2f}% win {sum(x > 0 for x in pnl) / len(pnl):.0%}")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("day", "daemon", "report", "scan"))
    ap.add_argument("--date", help="scan only: YYYY-MM-DD")
    args = ap.parse_args()
    if args.cmd == "scan":  # dry: print what the rule would have flagged on a past day, no orders
        for s in signals(date.fromisoformat(args.date)):
            print(s["ticker"], round(s["first_hour_excess"] * 100, 1), s["last"], s["headline"][:80])
        return
    {"day": day, "daemon": daemon, "report": report}[args.cmd]()


if __name__ == "__main__":
    main()
