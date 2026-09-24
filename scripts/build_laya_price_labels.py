"""Label every (news, ticker) pair in the analytics DB with what the price did in the next hour.

    python scripts/build_laya_price_labels.py [--db data/trades.db] [--threshold 1.0]

Prices are Yahoo Finance 60-minute bars (5-minute bars only go back 60 days). For news at time t:
  entry = close of the hourly bar that contains t   (first price known to be after the news)
  exit  = close of the following bar                (one hour later, same session only)
  excess = ticker return - SPY return over the same two bars
Label: buy if excess >= +threshold%, short if <= -threshold%, else hold.
Output: data/laya_price_labels.jsonl (one row per pair) and a bar cache in data/yahoo_60m/.
"""
import argparse
import json
import sqlite3
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CACHE = ROOT / "data" / "yahoo_60m"
URL = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=60m&period1={p1}&period2={p2}"


def fetch_bars(sym: str, start: datetime, end: datetime) -> list[tuple[int, float]]:
    """[(bar_start_epoch, close)] for regular-session hourly bars, cached on disk."""
    path = CACHE / f"{sym.replace('/', '_')}.json"
    if path.exists():
        return [tuple(b) for b in json.loads(path.read_text())]
    url = URL.format(sym=sym.replace(".", "-"), p1=int(start.timestamp()), p2=int(end.timestamp()))
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    bars: list[tuple[int, float]] = []
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                result = (json.load(resp).get("chart") or {}).get("result")
            if result and result[0].get("timestamp"):
                closes = result[0]["indicators"]["quote"][0]["close"]
                bars = [(t, c) for t, c in zip(result[0]["timestamp"], closes) if c is not None]
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                break
            time.sleep(2 ** attempt)
        except Exception:
            time.sleep(2 ** attempt)
    path.write_text(json.dumps(bars))
    time.sleep(0.15)
    return bars


def entry_exit(bars: list[tuple[int, float]], ts: int) -> tuple[float, float] | None:
    """Close of the bar containing ts, and close of the next bar if it is in the same session."""
    for i, (start, close) in enumerate(bars):
        nxt = bars[i + 1] if i + 1 < len(bars) else None
        if start <= ts and (nxt is None or ts < nxt[0]):
            if ts - start >= 3600 or nxt is None:
                return None  # after the last bar of the session
            if datetime.fromtimestamp(nxt[0], timezone.utc).date() != datetime.fromtimestamp(start, timezone.utc).date():
                return None  # next bar is the next day: overnight, not a 1h move
            return close, nxt[1]
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(ROOT / "data" / "trades.db"))
    ap.add_argument("--threshold", type=float, default=1.0, help="excess return in percent")
    ap.add_argument("--out", default=str(ROOT / "data" / "laya_price_labels.jsonl"))
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)

    con = sqlite3.connect(args.db)
    news = con.execute("SELECT id, ts, headline, summary, symbols FROM news_events ORDER BY ts").fetchall()
    stamps = [datetime.fromisoformat(r[1]) for r in news]
    start, end = min(stamps) - timedelta(days=1), max(stamps) + timedelta(days=2)
    tickers = sorted({s.strip().upper() for r in news for s in (r[4] or "").split(",") if s.strip()})
    print(f"{len(news)} news, {len(tickers)} tickers, {start:%Y-%m-%d} .. {end:%Y-%m-%d}", flush=True)

    spy = fetch_bars("SPY", start, end)
    bars = {}
    for i, sym in enumerate(tickers, 1):
        bars[sym] = fetch_bars(sym, start, end)
        if i % 200 == 0:
            print(f"  fetched {i}/{len(tickers)}", flush=True)

    counts = {"buy": 0, "short": 0, "hold": 0, "no_price": 0}
    with open(args.out, "w") as f:
        for (nid, ts, headline, summary, symbols), dt in zip(news, stamps):
            t = int(dt.timestamp())
            m = entry_exit(spy, t)
            for sym in dict.fromkeys(s.strip().upper() for s in (symbols or "").split(",") if s.strip()):
                p = entry_exit(bars.get(sym, []), t)
                if m is None or p is None:
                    counts["no_price"] += 1
                    continue
                ret = p[1] / p[0] - 1
                excess = ret - (m[1] / m[0] - 1)
                label = "buy" if excess * 100 >= args.threshold else "short" if excess * 100 <= -args.threshold else "hold"
                counts[label] += 1
                f.write(json.dumps({
                    "news_id": nid, "ts": ts, "ticker": sym, "headline": headline, "summary": summary or "",
                    "n_tickers": len((symbols or "").split(",")), "ret_1h": round(ret, 5),
                    "excess_1h": round(excess, 5), "label": label,
                }) + "\n")
    print(json.dumps(counts), "->", args.out)


if __name__ == "__main__":
    main()
