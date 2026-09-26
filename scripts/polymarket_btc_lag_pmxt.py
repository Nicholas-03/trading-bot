"""Out-of-sample check of the Bitcoin up/down latency rule on PMXT order-book hours (other days than the marketlens sample).

    .venv/bin/python scripts/polymarket_btc_lag_pmxt.py 2026-03-01T12 2026-03-05T18 ... [--fee 0.0175]

For each hour: the 5- and 15-minute "Bitcoin Up or Down" markets that open and close inside it (gamma slugs
btc-updown-{5m,15m}-<start unix>), their Up-token book rebuilt from the PMXT archive (snapshots + level changes), and
Binance 1-second BTCUSDT closes. Same rule, latency and fee as scripts/polymarket_btc_lag.py.
"""
import argparse
import json
import subprocess
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

sys.path.insert(0, str(Path(__file__).resolve().parent))
import polymarket_btc_lag as lag  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
TMP = ROOT / "data" / "pmxt"
UA = {"User-Agent": "Mozilla/5.0"}


def get(url: str):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return json.load(r)


def markets(hour_start: int) -> list[dict]:
    out = []
    for step, series in ((300, "btc-up-or-down-5m"), (900, "btc-up-or-down-15m")):
        for t0 in range(hour_start, hour_start + 3600, step):
            name = series.replace("btc-up-or-down", "btc-updown")
            d = get(f"https://gamma-api.polymarket.com/markets?slug={name}-{t0}&closed=true")
            if d:
                m = d[0]
                out.append({"series": series, "market": m["conditionId"], "up_token": json.loads(m["clobTokenIds"])[0],
                            "up": json.loads(m["outcomePrices"])[0] == "1", "t0": t0 * 1000, "t1": (t0 + step) * 1000})
    return out


def binance(start_s: int, end_s: int) -> lag.Reference:
    t, p = [], []
    for a in range(start_s, end_s, 1000):
        rows = get(f"https://data-api.binance.vision/api/v3/klines?symbol=BTCUSDT&interval=1s&startTime={a * 1000}"
                   f"&endTime={min(end_s, a + 1000) * 1000 - 1}&limit=1000")
        t += [r[0] + 999 for r in rows]  # the close is the last trade of that second
        p += [float(r[4]) for r in rows]
    return lag.Reference(np.array(t), np.array(p))


def up_book(path: Path, cid: set[str], tokens: set[str]) -> dict[str, tuple[list[int], list[tuple]]]:
    f = pq.ParquetFile(path)
    raw = defaultdict(list)
    for g in range(f.num_row_groups):
        tbl = f.read_row_group(g, columns=["market_id", "data"])
        tbl = tbl.filter(pc.is_in(tbl["market_id"], value_set=pa.array(sorted(cid))))
        for r in tbl.column("data").to_pylist():
            d = json.loads(r)
            if d.get("token_id") in tokens:
                raw[d["token_id"]].append(d)
    out = {}
    for tok, evs in raw.items():
        evs.sort(key=lambda d: d["timestamp"])
        bids, asks, ts, states = {}, {}, [], []
        for d in evs:
            if d["update_type"] == "book_snapshot":
                bids = {float(p): float(s) for p, s in d["bids"]}
                asks = {float(p): float(s) for p, s in d["asks"]}
            elif d.get("change_price") is not None:
                book = bids if d.get("change_side") == "BUY" else asks
                price, size = float(d["change_price"]), float(d["change_size"])
                if size:
                    book[price] = size
                else:
                    book.pop(price, None)
            if bids and asks:
                bb, ba = max(bids), min(asks)
                ts.append(int(d["timestamp"] * 1000))
                states.append((bb, bids[bb], ba, asks[ba]))
        out[tok] = (ts, states)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("hours", nargs="+")
    ap.add_argument("--fee", type=float, default=0.0175)
    args = ap.parse_args()
    TMP.mkdir(parents=True, exist_ok=True)
    trades = {lat: defaultdict(list) for lat in (0.2, 1.0)}
    for hour in args.hours:
        start = int(datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp())
        ms_ = markets(start)
        if not ms_:
            print(f"{hour}: no BTC up/down markets", flush=True)
            continue
        path = TMP / f"polymarket_orderbook_{hour}.parquet"
        if not path.exists():
            if subprocess.run(["curl", "-s", "-f", "-m", "900", "-o", str(path),
                               f"https://r2.pmxt.dev/polymarket_orderbook_{hour}.parquet"]).returncode:
                print(f"{hour}: no order-book file", flush=True)
                path.unlink(missing_ok=True)
                continue
        books = up_book(path, {m["market"] for m in ms_}, {m["up_token"] for m in ms_})
        path.unlink()
        ref = binance(start - 1200, start + 3600 + 60)
        n = 0
        for m in ms_:
            if m["up_token"] not in books:
                continue
            ts, states = books[m["up_token"]]
            n += 1
            for lat in trades:
                for thr, xs in lag.evaluate_market(ref, ts, states, m["t0"], m["t1"], m["up"], lat, args.fee,
                                                   {"series": m["series"], "market": m["market"], "hour": hour}).items():
                    trades[lat][thr] += xs
        print(f"{hour}: {len(ms_)} markets, {n} with books", flush=True)
    with open(ROOT / "results" / "polymarket_btc_lag_pmxt.jsonl", "w") as f:
        for lat, by in trades.items():
            for thr, xs in by.items():
                for x in xs:
                    f.write(json.dumps({"latency": lat, "threshold": thr, **x}) + "\n")
    for lat, by in trades.items():
        print(f"\n== latency {lat}s, taker fee {args.fee * 100:.2f}¢ at p=0.5")
        lag.report(by)


if __name__ == "__main__":
    main()
