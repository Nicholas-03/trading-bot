"""Forward paper test of the 5-day post-news drift (results/frozen_rule_drift5d.json, scripts/study_drift.py) on Alpaca PAPER.

    .venv/bin/python scripts/drift5d_paper.py daemon   # every trading day: intraday scan 10:32-15:44 ET, close run 15:46 ET
    .venv/bin/python scripts/drift5d_paper.py day      # the close run only (covers, drop_close entries)
    .venv/bin/python scripts/drift5d_paper.py intraday # the intraday scan only (until 15:44 ET)
    .venv/bin/python scripts/drift5d_paper.py report   # P&L per book
    .venv/bin/python scripts/drift5d_paper.py scan --date YYYY-MM-DD   # what the rule flags on a past day, no orders

The first regular-session news of each stock and day (09:31-15:30 ET) is scored exactly as in the backtest labels
(scripts/build_alpaca_labels.py label_pair): first hour = SPY-hedged return from the first 1-minute bar >= news + 60 s
to 60 minutes later (15:50 ET at the latest). Three books, each short NOTIONAL_USD per signal and covered at the close
5 trading days after the news day:
  drop_close  first hour <= -10%, short at the news day's close (the frozen rule)
  drop_hour   first hour <= -10%, short as soon as the first hour is over (backtest: +8.1% vs +7.0% at the close)
  pop_hour    first hour >= +10%, short as soon as the first hour is over (backtest, at the close: +5.2%)
A stock gets one real position at a time (Alpaca nets positions per symbol): the first book to fire trades it, later
books on the same stock are recorded as virtual trades at their own reference price, so all books are scored the
same way (reference price -> close of the exit day, minus SPY). Every signal is logged with Alpaca's shortable /
easy-to-borrow flags, since borrow availability is the main unknown. Refuses to run against a live account.
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
HOLD_DAYS = 5
NOTIONAL_USD = 2000.0
MAX_OPEN = 40  # real positions across books (~$80k of a $100k paper account)
MIN_PRICE = 2.0
BOOKS = {"drop_close": ("drop", "close"), "drop_hour": ("drop", "hour"), "pop_hour": ("pop", "hour")}


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


def first_hours(day: date, now_ts: float, skip: set[str] = frozenset()) -> list[dict]:
    """Scored first news of each stock today whose first hour is over by now_ts (stocks in `skip` are left out)."""
    news = [n for n in bal.fetch_news(day) if datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp() < now_ts]
    first: dict[str, dict] = {}
    for n in sorted(news, key=lambda n: n["created_at"]):
        for sym in dict.fromkeys(n["symbols"]):
            if sym.isascii() and sym.replace(".", "").isalpha():
                first.setdefault(sym, n)
    flatten = int(datetime(day.year, day.month, day.day, 15, 50, tzinfo=bal.ET).timestamp())
    due = {}
    for sym, n in first.items():
        ts = int(datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp())
        if sym not in skip and min(ts + 60 + 3660, flatten) <= now_ts:  # entry bar + 60 minutes are behind us
            due[sym] = (n, ts)
    if not due:
        return []
    bars = bal.fetch_bars(day, sorted(set(due) | {"SPY"}))
    spy = bars.get("SPY", {})
    out = []
    for sym, (n, ts) in due.items():
        b = bars.get(sym, {})
        lab = bal.label_pair(ts, b, spy, flatten) if spy and b else None
        if not lab:
            out.append({"ticker": sym, "scored": False})
            continue
        out.append({"ticker": sym, "scored": True, "news_id": n["id"], "news_ts": n["created_at"], "headline": n["headline"],
                    "first_hour_excess": lab["excess_1h"], "entry_1m": lab["entry"], "tradable_gate": lab["tradable"],
                    "last": b[max(b)][3], "spy_last": spy[max(spy)][3]})
    return out


def positions() -> dict[str, dict]:
    return {p["symbol"]: p for p in (trading("GET", "/positions") or []) if isinstance(p, dict)}


def enter(book: str, s: dict, today: date, tif: str) -> None:
    """Log the signal for `book`; short for real if the stock is free, shortable and within limits, else virtually."""
    evs = read_log()
    if any(e.get("event") == "signal" and e.get("book") == book and e["ticker"] == s["ticker"] and e["day"] == today.isoformat()
           for e in evs):
        return
    held = {e["ticker"] for e in evs if e.get("event") == "short" and not e.get("virtual")
            and (e.get("exit_day") or "9") >= today.isoformat()}
    asset = trading("GET", f"/assets/{s['ticker']}")
    shortable = bool(asset.get("shortable") and asset.get("easy_to_borrow"))
    ev = {"event": "signal", "book": book, "day": today.isoformat(), **s, "shortable": asset.get("shortable"),
          "easy_to_borrow": asset.get("easy_to_borrow")}
    real = False
    if s["last"] < MIN_PRICE:
        ev["skip"] = "price"
    elif not shortable:
        ev["skip"] = "not shortable at Alpaca"
    elif s["ticker"] in held:
        ev["note"] = "already short in another book (virtual)"
    elif len(held) >= MAX_OPEN:
        ev["note"] = "max open (virtual)"
    else:
        real = True
    log(ev)
    print(f"{datetime.now(bal.ET):%H:%M} {book:10s} {s['ticker']:6s} first hour {s['first_hour_excess'] * 100:+.1f}% "
          f"${s['last']:.2f} shortable={asset.get('shortable')} etb={asset.get('easy_to_borrow')} "
          f"{ev.get('skip') or ev.get('note') or 'SHORT'} | {s['headline'][:60]}", flush=True)
    if "skip" in ev:
        return
    qty = int(NOTIONAL_USD // s["last"])
    if qty < 1:
        return
    resp = trading("POST", "/orders", {"symbol": s["ticker"], "qty": str(qty), "side": "sell", "type": "market",
                                       "time_in_force": tif}) if real else None
    log({"event": "short", "book": book, "virtual": not real, "day": today.isoformat(), "exit_day": exit_date(today.isoformat()),
         "ticker": s["ticker"], "qty": qty, "ref_price": s["last"], "spy_ref": s["spy_last"],
         "entry": "close" if tif == "cls" else "hour", "order": resp})


def qualifies(kind: str, s: dict) -> bool:
    return s["scored"] and (s["first_hour_excess"] <= -0.10 if kind == "drop" else s["first_hour_excess"] >= 0.10)


def intraday(today: date | None = None) -> None:
    """Every minute until 15:44 ET: score first hours as they complete; hour books short right away."""
    today = today or datetime.now(bal.ET).date()
    if today.isoformat() not in trading_days(today, today):
        return
    done: set[str] = set()
    end = datetime(today.year, today.month, today.day, 15, 44, tzinfo=bal.ET).timestamp()
    while time.time() < end:
        try:
            for s in first_hours(today, time.time(), skip=done):
                done.add(s["ticker"])
                for book, (kind, when) in BOOKS.items():
                    if when == "hour" and qualifies(kind, s):
                        enter(book, s, today, "day")
        except Exception as exc:  # a bad minute must not stop the scan
            log({"event": "error", "where": "intraday", "error": repr(exc)})
            print("intraday error:", exc, flush=True)
        time.sleep(60)


def day(today: date | None = None) -> None:
    """Close run: cover due positions at the close, then enter the close book."""
    today = today or datetime.now(bal.ET).date()
    if today.isoformat() not in trading_days(today, today):
        print(today, "is not a trading day")
        return
    pos = positions()
    for e in read_log():
        if (e.get("event") == "short" and not e.get("virtual") and e.get("exit_day") and e["exit_day"] <= today.isoformat()
                and e["ticker"] in pos):
            p = pos.pop(e["ticker"])
            qty = abs(int(float(p["qty"])))
            resp = trading("POST", "/orders", {"symbol": e["ticker"], "qty": str(qty), "side": "buy", "type": "market",
                                               "time_in_force": "cls"})
            log({"event": "cover", "book": e.get("book"), "ticker": e["ticker"], "qty": qty, "entry_day": e["day"], "order": resp})
            print("cover", e["ticker"], qty, resp.get("status", resp), flush=True)
    for s in first_hours(today, time.time() + 3600):  # every first news of the day, windows ending by 15:50 at the latest
        for book, (kind, when) in BOOKS.items():
            if when == "close" and qualifies(kind, s):
                enter(book, s, today, "cls")


def daemon() -> None:
    while True:
        now = datetime.now(bal.ET)
        start = now.replace(hour=10, minute=32, second=0, microsecond=0)
        close = now.replace(hour=15, minute=46, second=0, microsecond=0)
        if now > close:
            start += timedelta(days=1)
            close += timedelta(days=1)
        if now < start:
            time.sleep((start - now).total_seconds())
        try:
            intraday()
            time.sleep(max(0.0, close.timestamp() - time.time()))
            day()
        except Exception as exc:  # keep the daemon alive; the next day retries covers too
            log({"event": "error", "error": repr(exc)})
            print("error:", exc, flush=True)
        time.sleep(60)


def report() -> None:
    evs = read_log()
    today = datetime.now(bal.ET).date().isoformat()
    for book in BOOKS:
        sig = [e for e in evs if e.get("event") == "signal" and e.get("book", "drop_close") == book]
        shorts = [e for e in evs if e.get("event") == "short" and e.get("book", "drop_close") == book]
        print(f"\n== {book}: {len(sig)} signals, {len(shorts)} shorts ({sum(not e.get('virtual') for e in shorts)} real); skips: "
              + ", ".join(f"{k} {sum(e.get('skip') == k for e in sig)}" for k in sorted({e.get('skip') for e in sig if e.get('skip')})))
        pnl = []
        for e in shorts:  # reference price -> close of the exit day (or latest), minus SPY's move
            end = min(e["exit_day"] or e["day"], today)
            d = bal.get("/v2/stocks/bars", {"symbols": f"{e['ticker']},SPY", "timeframe": "1Day", "start": e["day"],
                                             "end": end, "feed": "sip", "adjustment": "all"}).get("bars", {})
            c, spy = d.get(e["ticker"], []), d.get("SPY", [])
            if not c or not spy:
                continue
            ref, spy_ref = e.get("ref_price") or c[0]["c"], e.get("spy_ref") or spy[0]["c"]
            r = -((c[-1]["c"] / ref - 1) - (spy[-1]["c"] / spy_ref - 1)) * 100
            done = end >= (e["exit_day"] or "9") and c[-1]["t"][:10] >= end
            if done:
                pnl.append(r)
            print(f"  {e['day']} {e['ticker']:6s} {'virtual' if e.get('virtual') else 'real   '} short @ {ref:.2f} -> "
                  f"{c[-1]['c']:.2f}: {r:+.1f}% vs SPY {'(closed)' if done else '(open)'}")
        if pnl:
            print(f"  closed: n={len(pnl)} mean {sum(pnl) / len(pnl):+.2f}% win {sum(x > 0 for x in pnl) / len(pnl):.0%}")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("day", "intraday", "daemon", "report", "scan"))
    ap.add_argument("--date", help="scan only: YYYY-MM-DD")
    args = ap.parse_args()
    if args.cmd == "scan":  # dry run on a past day: what each book would have flagged (no orders)
        d = date.fromisoformat(args.date)
        for s in first_hours(d, datetime(d.year, d.month, d.day, 16, 0, tzinfo=bal.ET).timestamp()):
            books = [b for b, (k, _) in BOOKS.items() if qualifies(k, s)]
            if books:
                print(f"{s['ticker']:6s} {s['first_hour_excess'] * 100:+6.1f}% ${s['last']:.2f} {','.join(books)} | {s['headline'][:70]}")
        return
    {"day": day, "intraday": intraday, "daemon": daemon, "report": report}[args.cmd]()


if __name__ == "__main__":
    main()
