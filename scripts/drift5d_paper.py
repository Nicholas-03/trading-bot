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

Logs: results/drift5d_paper.jsonl (signals, orders, fills, covers, a daily account snapshot) and
results/drift5d_observed.jsonl (every scored first hour of the day, traded or not, with the news text and the borrow flags
at that moment: the part of the dataset that cannot be rebuilt from Alpaca's history later).
"""
import argparse
import json
import logging
import logging.handlers
import os
import re
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

DATA_DIR = Path(os.environ.get("DRIFT_DATA_DIR", ROOT / "results"))
LOG = DATA_DIR / "drift5d_paper.jsonl"
OBSERVED = DATA_DIR / "drift5d_observed.jsonl"  # every scored first hour, traded or not: a growing dataset
OUTCOMES = DATA_DIR / "drift5d_outcomes.jsonl"  # what happened next to each observed stock (filled in 5 days later)
HEARTBEAT = DATA_DIR / "heartbeat"  # touched every minute while the daemon is alive
JEV_MIN_MOVE = 0.05  # ask Jev about first hours at least this big (cheap: ~$0.00003 per event)
HOLD_DAYS = 5
NOTIONAL_USD = 2000.0
MAX_OPEN = 40  # real positions across books (~$80k of a $100k paper account)
MIN_PRICE = 2.0
SIP_DELAY = 16 * 60  # Alpaca's free data plan: SIP (all exchanges) bars only once they are 15+ minutes old
BOOKS = {"drop_close": ("drop", "close"), "drop_hour": ("drop", "hour"), "pop_halt": ("pop_halt", "close"),
         "pop_hour": ("pop", "hour")}
VIRTUAL_ONLY = {"pop_hour"}  # all pops: weak without a halt (+2.9%/trade, ~0 with a stop), tracked for comparison only
STOPS = {"pop_halt": 0.30}  # buy-stop 30% above the short's fill (backtest: +12.5% -> +9.1%/trade, t 5.1 -> 6.1)
HALT = re.compile(r"halted|resume|circuit breaker", re.I)  # same pattern as scripts/study_drift.py CATS
logger = logging.getLogger("drift5d")


def setup_logging() -> None:
    """Human-readable log to stdout (docker logs) and to DATA_DIR/logs/drift5d.log, rotated daily, 90 days kept."""
    (DATA_DIR / "logs").mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    fh = logging.handlers.TimedRotatingFileHandler(DATA_DIR / "logs" / "drift5d.log", when="midnight", backupCount=90)
    sh = logging.StreamHandler(sys.stdout)
    for h in (fh, sh):
        h.setFormatter(fmt)
        logger.addHandler(h)
    logger.setLevel(logging.INFO)


def beat() -> None:
    HEARTBEAT.parent.mkdir(parents=True, exist_ok=True)
    HEARTBEAT.write_text(datetime.now(timezone.utc).isoformat())


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
    LOG.parent.mkdir(parents=True, exist_ok=True)
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
    # Alpaca filters news by update time: keep stories written in the session (09:31-15:30 ET), as the backtest labels did
    lo = datetime(day.year, day.month, day.day, 9, 31, tzinfo=bal.ET).timestamp()
    hi = datetime(day.year, day.month, day.day, 15, 30, tzinfo=bal.ET).timestamp()
    news = [n for n in bal.fetch_news(day)
            if lo <= datetime.fromisoformat(n["created_at"].replace("Z", "+00:00")).timestamp() <= min(hi, now_ts)]
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
    bars, tail = live_bars(day, sorted(set(due) | {"SPY"}), now_ts)
    spy = bars.get("SPY", {})
    out = []
    for sym, (n, ts) in due.items():
        b = bars.get(sym, {})
        lab = bal.label_pair(ts, b, spy, flatten) if spy and b else None
        if not lab:
            out.append({"ticker": sym, "scored": False})
            continue
        out.append({"ticker": sym, "scored": True, "news_id": n["id"], "news_ts": n["created_at"], "headline": n["headline"],
                    "summary": n.get("summary") or "", "source": n.get("source") or "", "n_tickers": len(set(n["symbols"])),
                    "first_hour_excess": lab["excess_1h"], "entry_1m": lab["entry"], "tradable_gate": lab["tradable"],
                    "last": b[max(b)][3], "spy_last": spy[max(spy)][3], "last_bar": max(b), "iex_tail": tail})
    return out


def live_bars(day: date, symbols: list[str], now_ts: float) -> tuple[dict, bool]:
    """1-minute bars as in the backtest (SIP, all exchanges). Alpaca's free plan serves SIP only 15 minutes delayed,
    so the last SIP_DELAY minutes come from the real-time IEX feed (one exchange: thinner, but real trade prices)."""
    if time.time() - SIP_DELAY >= _ts(day, 16, 0):  # a past session: all SIP
        return bal.fetch_bars(day, symbols), False
    cut = int(time.time() - SIP_DELAY) // 60 * 60
    sip = fetch_bars_feed(symbols, _ts(day, 9, 0), cut, "sip")
    for sym, rows in fetch_bars_feed(symbols, cut, int(time.time()) + 60, "iex").items():
        m = sip.setdefault(sym, {})
        for t, bar in rows.items():
            m.setdefault(t, bar)
    return sip, True


def _ts(day: date, h: int, m: int) -> int:
    return int(datetime(day.year, day.month, day.day, h, m, tzinfo=bal.ET).timestamp())


def fetch_bars_feed(symbols: list[str], start: int, end: int, feed: str, timeframe: str = "1Min",
                    adjustment: str = "raw") -> dict[str, dict[int, tuple]]:
    """{symbol: {minute_epoch: (o, h, l, c, v)}} for [start, end) from one feed; invalid symbols are dropped by bisection."""
    bars: dict[str, dict[int, tuple]] = {}
    chunks = [symbols[i:i + 200] for i in range(0, len(symbols), 200)]
    while chunks:
        chunk = chunks.pop()
        params = {"symbols": ",".join(chunk), "timeframe": timeframe, "limit": 10000, "feed": feed, "adjustment": adjustment,
                  "start": datetime.fromtimestamp(start, timezone.utc).isoformat(),
                  "end": datetime.fromtimestamp(end, timezone.utc).isoformat()}
        token = None
        while True:
            try:
                d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except urllib.error.HTTPError as e:
                if e.code != 400 or token:
                    raise
                if len(chunk) > 1:
                    chunks += [chunk[: len(chunk) // 2], chunk[len(chunk) // 2:]]
                break
            for sym, rows in (d.get("bars") or {}).items():
                m = bars.setdefault(sym, {})
                for b in rows:
                    m[int(datetime.fromisoformat(b["t"].replace("Z", "+00:00")).timestamp())] = (b["o"], b["h"], b["l"], b["c"], b["v"])
            token = d.get("next_page_token")
            if not token:
                break
    return bars


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
    elif book in VIRTUAL_ONLY:
        ev["note"] = "comparison book (virtual)"
    elif s["ticker"] in held:
        ev["note"] = "already short in another book (virtual)"
    elif len(held) >= MAX_OPEN:
        ev["note"] = "max open (virtual)"
    else:
        real = True
    log(ev)
    logger.info(f"SIGNAL {book:10s} {s['ticker']:6s} first hour {s['first_hour_excess'] * 100:+.1f}% "
                f"${s['last']:.2f} shortable={asset.get('shortable')} etb={asset.get('easy_to_borrow')} "
                f"-> {ev.get('skip') or ev.get('note') or 'SHORT'} | {s['headline'][:70]}")
    if "skip" in ev:
        return
    qty = int(NOTIONAL_USD // s["last"])
    if qty < 1:
        return
    resp = trading("POST", "/orders", {"symbol": s["ticker"], "qty": str(qty), "side": "sell", "type": "market",
                                       "time_in_force": tif}) if real else None
    log({"event": "short", "book": book, "virtual": not real, "day": today.isoformat(), "exit_day": exit_date(today.isoformat()),
         "ticker": s["ticker"], "qty": qty, "ref_price": s["last"], "spy_ref": s["spy_last"],
         "entry": "close" if tif == "cls" else "hour", "stop": STOPS.get(book), "order": resp})
    if real:
        logger.info(f"ORDER short {qty} {s['ticker']} ({book}, {tif}): {resp.get('status') or resp}")


def observe(s: dict, today: date, when: str) -> None:
    """Append a scored first hour to OBSERVED with Alpaca's borrow flags at that moment (not recoverable later)."""
    asset = trading("GET", f"/assets/{s['ticker']}") if s["scored"] else {}
    jev = jev_features(s) if s["scored"] and abs(s["first_hour_excess"]) >= JEV_MIN_MOVE else {}
    with open(OBSERVED, "a") as f:
        f.write(json.dumps({"logged": datetime.now(timezone.utc).isoformat(), "day": today.isoformat(), "when": when, **s,
                            "shortable": asset.get("shortable"), "easy_to_borrow": asset.get("easy_to_borrow"),
                            "marginable": asset.get("marginable"), "exchange": asset.get("exchange"), **jev}) + "\n")


def jev_features(s: dict) -> dict:
    """TypeSafe Jev's reading of the news (same questions as scripts/jev_drift.py), for a forward test of Jev filters."""
    if not os.environ.get("TYPESAFE_API_KEY"):
        return {}
    import jev_drift
    import jev_score
    state = {"ticker": s["ticker"], "headline": s["headline"], "summary": (s.get("summary") or "")[:2000],
             "first_hour_move": f"{s['first_hour_excess'] * 100:+.0f}% relative to the overall market in the hour after the news"}
    try:
        a = jev_score.ask_body({"model": jev_score.MODEL, "state": state,
                                "questions": jev_drift.questions(s["first_hour_excess"] < 0)}, attempts=3)
    except Exception as exc:  # Jev is optional: record the failure and move on
        return {"jev_error": repr(exc)[:200]}
    ans = a["answers"]
    return {"jev_model": a["model"], "jev_category": ans["category"]["choice"], "jev_category_p": ans["category"]["probabilities"],
            "jev_next_week": ans["next_week"]["probabilities"], "jev_explains": ans["explains"]["noul"],
            "jev_lasting": ans["lasting"]["noul"], "jev_dilution": ans["dilution"]["noul"]}


def observed_today(today: date) -> set[str]:
    if not OBSERVED.exists():
        return set()
    return {r["ticker"] for r in map(json.loads, open(OBSERVED)) if r["day"] == today.isoformat()}


def check_fills() -> None:
    """Log the outcome (fill price, or cancel/reject) of every real order that has none logged yet."""
    evs = read_log()
    settled = {e["order_id"] for e in evs if e.get("event") == "fill"}
    for e in evs:
        oid = (e.get("order") or {}).get("id")
        if e.get("event") in ("short", "cover", "stop_placed") and oid and oid not in settled:
            o = trading("GET", f"/orders/{oid}")
            if o.get("status") in ("filled", "canceled", "expired", "rejected", "done_for_day"):
                log({"event": "fill", "order_id": oid, "book": e.get("book"), "ticker": e["ticker"], "side": o.get("side"),
                     "status": o["status"], "filled_qty": o.get("filled_qty"), "filled_avg_price": o.get("filled_avg_price"),
                     "filled_at": o.get("filled_at"), "for": e["event"], "day": e.get("day") or e.get("entry_day")})
                if e["event"] == "stop_placed" and o["status"] == "filled":
                    logger.info(f"STOPPED OUT {e['ticker']} ({e.get('book')}) at {o.get('filled_avg_price')}")


def qualifies(kind: str, s: dict) -> bool:
    if not s["scored"]:
        return False
    if kind == "drop":
        return s["first_hour_excess"] <= -0.10
    return s["first_hour_excess"] >= 0.10 and (kind == "pop" or bool(HALT.search(s["headline"])))


def place_stops() -> None:
    """Buy-stop orders (GTC) for filled real shorts in books with a stop, once per entry."""
    evs = read_log()
    fills = {e["order_id"]: e for e in evs if e.get("event") == "fill"}
    placed = {e["entry_order"] for e in evs if e.get("event") == "stop_placed"}
    pos = None
    for e in evs:
        oid = (e.get("order") or {}).get("id")
        if e.get("event") != "short" or e.get("virtual") or not e.get("stop") or not oid or oid in placed:
            continue
        f = fills.get(oid)
        if not f or f["status"] != "filled" or not f.get("filled_avg_price"):
            continue
        pos = pos if pos is not None else positions()
        if e["ticker"] not in pos:
            continue
        level = round(float(f["filled_avg_price"]) * (1 + e["stop"]), 2)
        qty = abs(int(float(pos[e["ticker"]]["qty"])))
        resp = trading("POST", "/orders", {"symbol": e["ticker"], "qty": str(qty), "side": "buy", "type": "stop",
                                           "stop_price": str(level), "time_in_force": "gtc"})
        log({"event": "stop_placed", "book": e["book"], "ticker": e["ticker"], "entry_order": oid, "qty": qty,
             "stop_price": level, "day": e["day"], "order": resp})
        logger.info(f"ORDER stop buy {qty} {e['ticker']} @ {level} ({e['book']}): {resp.get('status') or resp}")


def cancel_open_orders(symbol: str) -> None:
    """Before covering at the close: an open stop order would hold the shares and block the cover."""
    for o in trading("GET", "/orders", params={"status": "open", "symbols": symbol}) or []:
        if isinstance(o, dict) and o.get("id"):
            trading("DELETE", f"/orders/{o['id']}")
            log({"event": "cancel", "ticker": symbol, "order_id": o["id"], "type": o.get("type")})


def intraday(today: date | None = None) -> None:
    """Every minute until 15:44 ET: score first hours as they complete; hour books short right away."""
    today = today or datetime.now(bal.ET).date()
    if today.isoformat() not in trading_days(today, today):
        return
    done = observed_today(today)  # after a restart, carry on where the scan left off
    logger.info(f"intraday scan starts ({len(done)} stocks already observed today)")
    end = datetime(today.year, today.month, today.day, 15, 44, tzinfo=bal.ET).timestamp()
    last_note = 0.0
    while time.time() < end:
        beat()
        try:
            new = first_hours(today, time.time(), skip=done)
            for s in new:
                done.add(s["ticker"])
                observe(s, today, "hour")
                for book, (kind, when) in BOOKS.items():
                    if when == "hour" and qualifies(kind, s):
                        enter(book, s, today, "day")
            big = [f"{s['ticker']} {s['first_hour_excess'] * 100:+.0f}%" for s in new
                   if s["scored"] and abs(s["first_hour_excess"]) >= JEV_MIN_MOVE]
            if big or time.time() - last_note > 1800:  # moves worth seeing, else a status line every 30 min
                logger.info(f"scan: {len(new)} new first hours, {len(done)} observed today"
                            + (f"; >=5%: {', '.join(big)}" if big else ""))
                last_note = time.time()
        except Exception as exc:  # a bad minute must not stop the scan
            log({"event": "error", "where": "intraday", "error": repr(exc)})
            logger.exception("intraday scan failed")
        time.sleep(60)


def day(today: date | None = None) -> None:
    """Close run: cover due positions at the close, then enter the close book."""
    today = today or datetime.now(bal.ET).date()
    if today.isoformat() not in trading_days(today, today):
        logger.info(f"{today} is not a trading day")
        return
    logger.info("close run")
    check_fills()
    place_stops()
    acct = trading("GET", "/account")
    log({"event": "account", "day": today.isoformat(), **{k: acct.get(k) for k in
         ("equity", "cash", "long_market_value", "short_market_value", "buying_power")}})
    pos = positions()
    for e in read_log():
        if (e.get("event") == "short" and not e.get("virtual") and e.get("exit_day") and e["exit_day"] <= today.isoformat()
                and e["ticker"] in pos):
            p = pos.pop(e["ticker"])
            qty = abs(int(float(p["qty"])))
            cancel_open_orders(e["ticker"])
            resp = trading("POST", "/orders", {"symbol": e["ticker"], "qty": str(qty), "side": "buy", "type": "market",
                                               "time_in_force": "cls"})
            log({"event": "cover", "book": e.get("book"), "ticker": e["ticker"], "qty": qty, "entry_day": e["day"], "order": resp})
            logger.info(f"ORDER cover {qty} {e['ticker']} ({e.get('book')}, entered {e['day']}): {resp.get('status') or resp}")
    seen = observed_today(today)
    for s in first_hours(today, time.time() + 3600):  # every first news of the day, windows ending by 15:50 at the latest
        if s["ticker"] not in seen:
            observe(s, today, "close")
        for book, (kind, when) in BOOKS.items():
            if when == "close" and qualifies(kind, s):
                enter(book, s, today, "cls")
    logger.info(f"account: equity {acct.get('equity')} cash {acct.get('cash')} short value {acct.get('short_market_value')}; "
                f"{len(observed_today(today))} stocks observed today")


def label_outcomes(today: date) -> None:
    """Dataset: for each observed day whose 5-day window has closed, what each stock did next (split-adjusted daily
    bars, SIP): news-day close, next open/close, close 5 trading days later, the 5-day high and low, SPY and IWM."""
    if not OBSERVED.exists():
        return
    done = {r["day"] for r in map(json.loads, open(OUTCOMES))} if OUTCOMES.exists() else set()
    by_day: dict[str, dict[str, dict]] = {}
    for r in map(json.loads, open(OBSERVED)):
        if r.get("scored") and r["day"] not in done:
            by_day.setdefault(r["day"], {}).setdefault(r["ticker"], r)
    for d, obs in sorted(by_day.items()):
        exit_day = exit_date(d)
        if not exit_day or exit_day >= today.isoformat():
            continue
        d0 = date.fromisoformat(d)
        bars = fetch_bars_feed(sorted(set(obs) | {"SPY", "IWM"}), _ts(d0, 0, 0), _ts(date.fromisoformat(exit_day), 23, 0),
                               "sip", timeframe="1Day", adjustment="all")
        daykey = lambda t: datetime.fromtimestamp(t, bal.ET).date().isoformat()
        series = {sym: {daykey(t): b for t, b in rows.items()} for sym, rows in bars.items()}
        days = [x for x in trading_days(d0, date.fromisoformat(exit_day))]
        n = 0
        with open(OUTCOMES, "a") as f:
            for sym, r in obs.items():
                b = series.get(sym, {})
                if d not in b:
                    continue
                path = [b.get(x) for x in days]
                later = [p for p in path[1:] if p]
                out = {"day": d, "ticker": sym, "news_id": r.get("news_id"), "first_hour_excess": r.get("first_hour_excess"),
                       "exit_day": exit_day, "c0": b[d][3], "v0": b[d][4],
                       "o1": path[1][0] if len(path) > 1 and path[1] else None,
                       "c1": path[1][3] if len(path) > 1 and path[1] else None,
                       "c5": b[exit_day][3] if exit_day in b else None,
                       "hi5": max((p[1] for p in later), default=None), "lo5": min((p[2] for p in later), default=None)}
                for idx in ("SPY", "IWM"):
                    ib = series.get(idx, {})
                    out[idx.lower()] = [ib[x][3] if x in ib else None for x in (d, days[1] if len(days) > 1 else d, exit_day)]
                f.write(json.dumps(out) + "\n")
                n += 1
            f.write(json.dumps({"day": d, "marker": "day labeled", "n": n}) + "\n")
        logger.info(f"outcomes: labeled {n} stocks observed on {d} (exit {exit_day})")


def sleep_until(ts: float) -> None:
    """Sleep in short steps against the wall clock (one long sleep can overrun, e.g. across a system sleep)."""
    while time.time() < ts:
        beat()
        time.sleep(min(60.0, max(0.0, ts - time.time())))


def daemon() -> None:
    pre = "ALPACA_PAPER2_" if os.environ.get("ALPACA_PAPER2_API_KEY") else "ALPACA_"
    acct = trading("GET", "/account")
    logger.info(f"daemon up: data dir {DATA_DIR}, paper account via {pre}*, equity {acct.get('equity')}, "
                f"shorting {acct.get('shorting_enabled')}, Jev {'on' if os.environ.get('TYPESAFE_API_KEY') else 'off'}")
    while True:
        now = datetime.now(bal.ET)
        pre_open = now.replace(hour=9, minute=20, second=0, microsecond=0)
        start = now.replace(hour=10, minute=32, second=0, microsecond=0)
        close = now.replace(hour=15, minute=46, second=0, microsecond=0)
        if now > close:
            pre_open += timedelta(days=1)
            start += timedelta(days=1)
            close += timedelta(days=1)
        if now < pre_open:
            sleep_until(pre_open.timestamp())
            try:  # yesterday's closing fills are known now: protect them before the open
                check_fills()
                place_stops()
            except Exception:
                logger.exception("pre-open stops failed")
        sleep_until(start.timestamp())
        try:
            intraday()
            sleep_until(close.timestamp())
            day()
            label_outcomes(datetime.now(bal.ET).date())
        except Exception as exc:  # keep the daemon alive; the next day retries covers too
            log({"event": "error", "error": repr(exc)})
            logger.exception("daily run failed")
        logger.info("day done; sleeping until the next session")
        time.sleep(60)


def report() -> None:
    check_fills()
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
            exit_px, k = c[-1]["c"], len(c) - 1
            if e.get("stop"):  # as in the backtest: stopped on the first later day whose high reaches the level
                level = ref * (1 + e["stop"])
                hit = next((i for i in range(1, len(c)) if c[i]["h"] >= level), None)
                if hit is not None:
                    exit_px, k = max(level, c[hit]["o"]), hit
            spy_k = spy[min(k, len(spy) - 1)]["c"]
            r = -((exit_px / ref - 1) - (spy_k / spy_ref - 1)) * 100
            done = end >= (e["exit_day"] or "9") and c[-1]["t"][:10] >= end or k < len(c) - 1
            if done:
                pnl.append(r)
            print(f"  {e['day']} {e['ticker']:6s} {'virtual' if e.get('virtual') else 'real   '} short @ {ref:.2f} -> "
                  f"{exit_px:.2f}{' (stopped)' if k < len(c) - 1 else ''}: {r:+.1f}% vs SPY {'(closed)' if done else '(open)'}")
        if pnl:
            print(f"  closed: n={len(pnl)} mean {sum(pnl) / len(pnl):+.2f}% win {sum(x > 0 for x in pnl) / len(pnl):.0%}")


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=("day", "intraday", "daemon", "report", "scan", "label"))
    ap.add_argument("--date", help="scan only: YYYY-MM-DD")
    args = ap.parse_args()
    if args.cmd == "scan":  # dry run on a past day: what each book would have flagged (no orders)
        d = date.fromisoformat(args.date)
        for s in first_hours(d, datetime(d.year, d.month, d.day, 16, 0, tzinfo=bal.ET).timestamp()):
            books = [b for b, (k, _) in BOOKS.items() if qualifies(k, s)]
            if books:
                print(f"{s['ticker']:6s} {s['first_hour_excess'] * 100:+6.1f}% ${s['last']:.2f} {','.join(books)} | {s['headline'][:70]}")
        return
    setup_logging()
    if args.cmd == "label":
        label_outcomes(datetime.now(bal.ET).date())
        return
    {"day": day, "intraday": intraday, "daemon": daemon, "report": report}[args.cmd]()


if __name__ == "__main__":
    main()
