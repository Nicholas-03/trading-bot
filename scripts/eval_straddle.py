"""Would an at-the-money straddle on the news Laya expects to move the most have made money?

    .venv/bin/python scripts/eval_straddle.py kaggle/laya-trading/results/f_ret_react/test_predictions.jsonl [--per-group 300]

Laya's fine-tuned models rank the size of the move after news well but not its direction, which is exactly what a straddle
(long call + long put, same strike) is paid for. For each chosen event: nearest expiry after the news day, strike closest to
the stock's entry price, both legs priced from Alpaca's 1-minute option trade bars (first print in the 5 minutes after entry,
last print in the 10 minutes before entry + 60 min). Groups: top 10% by predicted move size vs a random sample of the rest.
Option bars are trade prints, not quotes, so the bid/ask spread is charged separately (net at 4% and 8% of premium).
"""
import argparse
import json
import os
import random
import sys
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402

TRADING = "https://paper-api.alpaca.markets"


def contracts(ticker: str, day: date, price: float) -> list[dict]:
    """Contracts expiring 1..35 days after the news day with a strike within 10% of the price (expired ones are "inactive")."""
    out = []
    for status in ("inactive", "active"):
        q = {"underlying_symbols": ticker, "status": status, "expiration_date_gte": str(day + timedelta(days=1)),
             "expiration_date_lte": str(day + timedelta(days=35)), "strike_price_gte": round(price * 0.9, 2),
             "strike_price_lte": round(price * 1.1, 2), "limit": 10000}
        bal.LIMIT.wait()
        req = urllib.request.Request(f"{TRADING}/v2/options/contracts?{urllib.parse.urlencode(q)}", headers={
            "APCA-API-KEY-ID": os.environ["ALPACA_API_KEY"], "APCA-API-SECRET-KEY": os.environ["ALPACA_SECRET_KEY"]})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                out += json.load(resp).get("option_contracts") or []
        except urllib.error.HTTPError as e:
            if e.code not in (400, 404, 422, 429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError):
            pass
    return out


def straddle(ev: dict) -> dict | None:
    ts = datetime.fromisoformat(ev["ts"].replace("Z", "+00:00"))
    day = ts.astimezone(bal.ET).date()
    entry_t = datetime.fromtimestamp(((int(ts.timestamp()) + bal.LATENCY_S + 59) // 60) * 60, timezone.utc)
    cs = contracts(ev["ticker"], day, ev["entry"])
    if not cs:
        return None
    expiry = min(c["expiration_date"] for c in cs)
    chain = [c for c in cs if c["expiration_date"] == expiry]
    strikes = {float(c["strike_price"]) for c in chain if c["type"] == "call"} & \
              {float(c["strike_price"]) for c in chain if c["type"] == "put"}
    if not strikes:
        return None
    k = min(strikes, key=lambda s: abs(s - ev["entry"]))
    legs = [c["symbol"] for c in chain if float(c["strike_price"]) == k]
    exit_t = min(entry_t + timedelta(minutes=60), datetime(day.year, day.month, day.day, 15, 50, tzinfo=bal.ET))
    d = bal.get("/v1beta1/options/bars", {"symbols": ",".join(legs), "timeframe": "1Min", "limit": 1000,
                                          "start": entry_t.isoformat(), "end": exit_t.isoformat()})
    bars = d.get("bars") or {}
    p0 = p1 = 0.0
    for sym in legs:
        rows = [(datetime.fromisoformat(b["t"].replace("Z", "+00:00")), b) for b in bars.get(sym, [])]
        first = [b for t, b in rows if t < entry_t + timedelta(minutes=5)]
        last = [b for t, b in rows if t >= exit_t - timedelta(minutes=10)]
        if not first or not last:
            return None
        p0, p1 = p0 + first[0]["o"], p1 + last[-1]["c"]
    return {**ev, "strike": k, "expiry": expiry, "dte": (date.fromisoformat(expiry) - day).days,
            "premium_pct": round(p0 / ev["entry"] * 100, 2), "ret": round((p1 / p0 - 1) * 100, 2)}


def safe_straddle(ev: dict) -> dict | None:
    try:
        return straddle(ev)
    except bal.urllib.error.HTTPError as e:  # e.g. a symbol the options API rejects
        if e.code in (400, 404, 422):
            return None
        raise


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("predictions")
    ap.add_argument("--labels", default=str(ROOT / "data" / "laya_alpaca_labels_v2.jsonl"))
    ap.add_argument("--per-group", type=int, default=300)
    ap.add_argument("--out", default=str(ROOT / "results" / "straddle_trades.jsonl"))
    args = ap.parse_args()

    mag = {}
    for line in open(args.predictions):
        p = json.loads(line)
        if p["model"] == "laya_ft":
            mag[(p["news_id"], p["ticker"])] = 1 - p["probs"]["hold"]
    events, seen = [], set()
    for line in open(args.labels):
        r = json.loads(line)
        key = (r["news_id"], r["ticker"])
        if key in mag and r["tradable"] and r.get("ret_1h") is not None and (r["ticker"], r["ts"][:16]) not in seen:
            seen.add((r["ticker"], r["ts"][:16]))
            events.append({"news_id": r["news_id"], "ticker": r["ticker"], "ts": r["ts"], "entry": r["entry"],
                           "ret_1h": r["ret_1h"], "mag": mag[key]})
    cut = np.quantile([e["mag"] for e in events], 0.9)
    rng = random.Random(0)
    top = [e for e in events if e["mag"] >= cut]
    rest = [e for e in events if e["mag"] < cut]
    chosen = [{**e, "group": "top10"} for e in rng.sample(top, min(args.per_group, len(top)))] + \
             [{**e, "group": "rest"} for e in rng.sample(rest, min(args.per_group, len(rest)))]
    print(f"{len(events)} events, magnitude cut {cut:.3f}; pricing {len(chosen)} straddles", flush=True)
    with ThreadPoolExecutor(4) as pool:
        res = [r for r in pool.map(safe_straddle, chosen) if r]
    Path(args.out).parent.mkdir(exist_ok=True)
    with open(args.out, "w") as f:
        for r in res:
            f.write(json.dumps(r) + "\n")
    for g in ("top10", "rest"):
        x = [r for r in res if r["group"] == g]
        if not x:
            continue
        ret = np.array([r["ret"] for r in x])
        mv = np.array([abs(r["ret_1h"]) * 100 for r in x])
        print(f"{g}: priced {len(x)}  |stock move| {mv.mean():.2f}%  premium {np.mean([r['premium_pct'] for r in x]):.1f}% "
              f"of stock  dte median {np.median([r['dte'] for r in x]):.0f}  straddle 60m: mean {ret.mean():+.2f}% "
              f"median {np.median(ret):+.2f}% ± {ret.std() / np.sqrt(len(ret)):.2f}  net@4% {ret.mean() - 4:+.2f}  "
              f"net@8% {ret.mean() - 8:+.2f}  win {np.mean(ret > 4):.0%}")


if __name__ == "__main__":
    main()
