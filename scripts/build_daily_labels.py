"""Next-day and 5-day labels for every news pair: does the news keep moving the stock after the day it came out?

    .venv/bin/python scripts/build_daily_labels.py

For each (news, ticker) in the intraday label files, with d = the news day (ET) and C = split-adjusted daily close:
  nd_ret / nd_excess  = C[d+1] / C[d] - 1  (minus SPY's)   buy at the news day's close, sell at the next close
  d5_ret / d5_excess  = C[d+5] / C[d] - 1  (minus SPY's)
  day_ret             = C[d] / C[d-1] - 1                  the news day's own move (known at entry)
Daily bars come from Alpaca (cached in data/daily_bars.json). Output: data/laya_daily_labels.jsonl, the same rows as
data/laya_labels_2021_2026.jsonl (tradable pairs) plus these fields.
"""
import json
import sys
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

BARS = ROOT / "data" / "daily_bars.json"


def fetch(symbols: list[str]) -> dict[str, dict[str, float]]:
    out: dict[str, dict[str, float]] = {}
    for i in range(0, len(symbols), 100):
        chunk = symbols[i:i + 100]
        params = {"symbols": ",".join(chunk), "timeframe": "1Day", "start": "2020-12-01", "end": "2026-09-27",
                  "limit": 10000, "feed": "sip", "adjustment": "all"}
        token = None
        while True:
            try:
                d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
            except bal.urllib.error.HTTPError as e:  # an invalid symbol in the chunk: fetch one by one
                if e.code != 400:
                    raise
                for s in chunk:
                    out.update(fetch_single(s))
                break
            for sym, rows in (d.get("bars") or {}).items():
                m = out.setdefault(sym, {})
                for b in rows:
                    m[b["t"][:10]] = b["c"]
            token = d.get("next_page_token")
            if not token:
                break
        print(f"{min(i + 100, len(symbols))}/{len(symbols)} symbols", flush=True)
    return out


def fetch_single(sym: str) -> dict[str, dict[str, float]]:
    out, token = {}, None
    params = {"symbols": sym, "timeframe": "1Day", "start": "2020-12-01", "end": "2026-09-27", "limit": 10000,
              "feed": "sip", "adjustment": "all"}
    while True:
        try:
            d = bal.get("/v2/stocks/bars", {**params, **({"page_token": token} if token else {})})
        except bal.urllib.error.HTTPError:
            return out
        for s, rows in (d.get("bars") or {}).items():
            out.setdefault(s, {}).update({b["t"][:10]: b["c"] for b in rows})
        token = d.get("next_page_token")
        if not token:
            return out


def main():
    load_dotenv(ROOT / ".env")
    rows = [json.loads(l) for l in open(ROOT / "data" / "laya_labels_2021_2026.jsonl")]
    if BARS.exists():
        bars = json.loads(BARS.read_text())
    else:
        bars = fetch(sorted({r["ticker"] for r in rows} | {"SPY"}))
        BARS.write_text(json.dumps(bars))
    spy = bars["SPY"]
    days = sorted(spy)
    idx = {d: i for i, d in enumerate(days)}
    n = 0
    with open(ROOT / "data" / "laya_daily_labels.jsonl", "w") as f:
        for r in rows:
            d = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(bal.ET).date().isoformat()
            c = bars.get(r["ticker"])
            if not c or d not in idx or idx[d] < 1 or idx[d] + 5 >= len(days):
                continue
            i = idx[d]
            d0, dm1, d1, d5 = days[i], days[i - 1], days[i + 1], days[i + 5]
            if not all(x in c for x in (d0, dm1, d1, d5)):
                continue
            s0 = spy[d0]
            r.update(nd_ret=c[d1] / c[d0] - 1, nd_excess=c[d1] / c[d0] - spy[d1] / s0,
                     d5_ret=c[d5] / c[d0] - 1, d5_excess=c[d5] / c[d0] - spy[d5] / s0,
                     day_ret=c[d0] / c[dm1] - 1, day_excess=c[d0] / c[dm1] - s0 / spy[dm1])
            f.write(json.dumps(r) + "\n")
            n += 1
    print(n, "rows -> data/laya_daily_labels.jsonl")


if __name__ == "__main__":
    main()
