"""liquid_fade events for 2016-2020 (before our intraday labels start), from daily bars and Alpaca news only.

    .venv/bin/python scripts/build_fade_history.py [--start 2016-01-01] [--end 2020-12-31]

1. Daily bars (split-adjusted, SIP) for every symbol in data/laya_daily_labels.jsonl plus Alpaca's inactive (delisted)
   US equities, so names that died before 2021 are not missing. Cached in data/daily_bars_hist.json.
2. Event: a day whose move against SPY (previous close -> close) is >= 8% either way, on a liquid stock: previous close
   >= $5 and median dollar volume over the 20 days before >= $5M (prev_close and dollar_vol are kept, to tighten later).
3. News: Alpaca news for the symbol created 09:31-15:30 ET that day (as the live trader sees it). One row per news item,
   in the format of data/laya_daily_labels.jsonl (day_excess, d5_excess, tradable), to data/laya_fade_labels_2016_2020.jsonl.
"""
import argparse
import json
import statistics
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

BARS = ROOT / "data" / "daily_bars_hist.json"
OUT = ROOT / "data" / "laya_fade_labels_2016_2020.jsonl"
MIN_MOVE, MIN_PRICE, MIN_DOLLAR_VOL = 0.08, 5.0, 5e6


def universe() -> list[str]:
    syms = {json.loads(line)["ticker"] for line in open(ROOT / "data" / "laya_daily_labels.jsonl")}
    import drift5d_paper as dp  # trading API helper (paper account; /assets is the same everywhere)
    for a in dp.trading("GET", "/assets", params={"status": "inactive", "asset_class": "us_equity"}) or []:
        if a.get("exchange") in ("NYSE", "NASDAQ", "AMEX", "ARCA", "BATS") and "." not in a["symbol"]:
            syms.add(a["symbol"])
    return sorted(syms | {"SPY"})


def fetch_bars(symbols: list[str], start: str, end: str) -> dict[str, dict[str, list]]:
    out: dict[str, dict[str, list]] = json.loads(BARS.read_text()) if BARS.exists() else {}
    todo = [s for s in symbols if s not in out]
    print(f"daily bars: {len(symbols)} symbols, {len(todo)} to fetch", flush=True)

    def chunk_bars(chunk):
        got, token = {s: {} for s in chunk}, None
        params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": start, "end": end, "limit": 10000,
                  "feed": "sip", "adjustment": "all"}
        while True:
            try:
                d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except bal.urllib.error.HTTPError as e:
                if e.code != 400 or len(chunk) == 1:
                    return {s: {} for s in chunk} if e.code == 400 else None
                merged = {}
                for s in chunk:  # an invalid symbol in the chunk: one by one
                    merged.update(chunk_bars([s]) or {})
                return merged
            for sym, rows in (d.get("bars") or {}).items():
                for b in rows:
                    got[sym][b["t"][:10]] = [b["c"], b["v"]]
            token = d.get("next_page_token")
            if not token:
                return got

    chunks = [todo[i:i + 100] for i in range(0, len(todo), 100)]
    for i, got in enumerate(ThreadPoolExecutor(4).map(chunk_bars, chunks)):
        out.update(got or {})
        if i % 10 == 9 or i == len(chunks) - 1:
            BARS.write_text(json.dumps(out))
            print(f"  {min((i + 1) * 100, len(todo))}/{len(todo)}", flush=True)
    return out


def events(bars: dict, start: str, end: str) -> list[dict]:
    spy = bars["SPY"]
    days = sorted(spy)
    idx = {d: i for i, d in enumerate(days)}
    out = []
    for sym, b in bars.items():
        if sym == "SPY":
            continue
        for d in b:
            i = idx.get(d)
            if i is None or i < 21 or i + 5 >= len(days) or not (start <= d <= end):
                continue
            p, n5 = days[i - 1], days[i + 5]
            if p not in b or n5 not in b:
                continue
            c0, cm1 = b[d][0], b[p][0]
            day_excess = c0 / cm1 - spy[d][0] / spy[p][0]
            if abs(day_excess) < MIN_MOVE or cm1 < MIN_PRICE:
                continue
            dv = [b[x][0] * b[x][1] for x in days[i - 20:i] if x in b]
            dollar_vol = statistics.median(dv) if len(dv) >= 15 else 0
            if dollar_vol < MIN_DOLLAR_VOL:
                continue
            d5 = b[n5][0] / c0 - spy[n5][0] / spy[d][0]
            if abs(d5) >= 1:  # split errors
                continue
            out.append({"ticker": sym, "day": d, "day_excess": round(day_excess, 5), "d5_excess": round(d5, 5),
                        "prev_close": cm1, "dollar_vol": round(dollar_vol)})
    return out


def news_rows(e: dict) -> list[dict]:
    y, m, dd = map(int, e["day"].split("-"))
    s = datetime(y, m, dd, 9, 31, tzinfo=bal.ET)
    t = datetime(y, m, dd, 15, 30, tzinfo=bal.ET)
    params = {"symbols": e["ticker"], "start": s.astimezone(timezone.utc).isoformat(),
              "end": t.astimezone(timezone.utc).isoformat(), "limit": 50, "sort": "asc", "include_content": "false"}
    rows = []
    for n in bal.get("/v1beta1/news", params).get("news", []):
        created = datetime.fromisoformat(n["created_at"].replace("Z", "+00:00"))
        if not s <= created <= t:  # the API filters by update time
            continue
        rows.append({"news_id": n["id"], "ts": created.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                     "ticker": e["ticker"], "headline": n["headline"], "summary": n.get("summary") or "",
                     "source": n.get("source") or "", "n_tickers": len(n.get("symbols") or []), "tradable": True,
                     "react": None, "excess_1h": 0.0, "ret_1h": 0.0, **{k: e[k] for k in
                     ("day_excess", "d5_excess", "prev_close", "dollar_vol")}})
    return rows


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", default="2016-01-01")
    ap.add_argument("--end", default="2020-12-31")
    args = ap.parse_args()
    syms = universe()
    bars = fetch_bars(syms, "2015-11-01", "2021-01-15")
    evs = events(bars, args.start, args.end)
    checked = OUT.with_suffix(".checked")
    seen = set(checked.read_text().split()) if checked.exists() else set()
    todo = [e for e in evs if f"{e['ticker']}:{e['day']}" not in seen]
    print(f"{len(evs)} liquid >= {MIN_MOVE:.0%} days {args.start}..{args.end}; {len(todo)} to look up news for", flush=True)
    lock, n_ev, n_news = threading.Lock(), 0, 0

    def one(e):
        nonlocal n_ev, n_news
        try:
            rows = news_rows(e)
        except Exception as exc:
            print("error", e["ticker"], e["day"], exc, flush=True)
            return
        with lock:
            with open(OUT, "a") as f:
                f.writelines(json.dumps(r) + "\n" for r in rows)
            with open(checked, "a") as f:
                f.write(f"{e['ticker']}:{e['day']}\n")
            n_ev += 1
            n_news += len(rows)
            if n_ev % 1000 == 0:
                print(f"  {n_ev}/{len(todo)} days, {n_news} news", flush=True)

    with ThreadPoolExecutor(4) as ex:
        list(ex.map(one, todo))
    print(f"done: {n_ev} days looked up, {n_news} news rows -> {OUT.name}", flush=True)


if __name__ == "__main__":
    main()
