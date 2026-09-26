"""Would a Polymarket market maker earn more from the spread than it loses to informed traders?

    .venv/bin/python scripts/polymarket_mm.py [--markets 400] [--min-volume 1000000]

For every public taker trade on a sample of days (3, 7 and 14 days before each market closed), the resting side (maker)
earns   maker_pnl(dt) = s * (price - mid(t + dt))   per share, s = +1 if the taker bought YES, -1 if they sold it.
That splits into the half-spread the maker collected, s * (price - mid(t)), minus adverse selection, s * (mid(t+dt) - mid(t)).
mid(T) = average of the latest taker-buy and taker-sell prices in the 10 minutes before T (about the bid/ask midpoint),
so no quotes are needed. Positive maker_pnl at 5-60 minutes means quoting both sides pays before liquidity rewards
(Polymarket charges makers no fee). Results are per share in cents, per trade and volume-weighted.
"""
import argparse
import bisect
import json
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import polymarket_calibration as pc  # noqa: E402  (market listing and HTTP helper)

DATA = "https://data-api.polymarket.com"
HORIZONS = (60, 300, 3600)


def day_trades(cid: str, start: int) -> list[tuple[int, int, float, float]]:
    """(time, taker sign on YES, YES price, size) for one day, oldest first."""
    out, offset = [], 0
    while offset < 3000:
        page = pc.get(f"{DATA}/trades", {"market": cid, "start": start, "end": start + 86400, "limit": 500,
                                         "offset": offset})
        if not isinstance(page, list) or not page:
            break
        for x in page:
            yes = x.get("outcomeIndex") == 0
            buy = x.get("side") == "BUY"
            price = float(x["price"]) if yes else 1 - float(x["price"])
            out.append((int(x["timestamp"]), 1 if buy == yes else -1, price, float(x["size"])))
        offset += len(page)
        if len(page) < 500:
            break
    return sorted(out)


def maker_pnl(trades: list[tuple]) -> list[dict]:
    t = [x[0] for x in trades]
    last = {1: [], -1: []}  # per taker side: (time, price), time-ordered
    for x in trades:
        last[x[1]].append((x[0], x[2]))
    times = {s: [y[0] for y in v] for s, v in last.items()}

    def mid(T):
        px = []
        for s in (1, -1):
            i = bisect.bisect_left(times[s], T) - 1
            if i < 0 or T - last[s][i][0] > 600:
                return None
            px.append(last[s][i][1])
        return sum(px) / 2

    out = []
    for tt, s, p, size in trades:
        if not 0.05 < p < 0.95:
            continue
        m0 = mid(tt)
        fut = {dt: mid(tt + dt) for dt in HORIZONS}
        if m0 is None or any(v is None for v in fut.values()) or tt + max(HORIZONS) > t[-1]:
            continue
        out.append({"t": tt, "size": size, "half_spread": s * (p - m0),
                    **{f"pnl{dt}": s * (p - fut[dt]) for dt in HORIZONS}})
    return out


def one(m: dict) -> list[dict]:
    rows = []
    for k in (3, 7, 14):
        start = int(m["closed"] - k * 86400) // 86400 * 86400
        for r in maker_pnl(day_trades(m["cid"], start)):
            rows.append({**r, "q": m["q"], "volume": m["volume"]})
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=400)
    ap.add_argument("--min-volume", type=float, default=1_000_000)
    ap.add_argument("--out", default=str(ROOT / "results" / "polymarket_mm.jsonl"))
    args = ap.parse_args()
    ms = [m for m in pc.markets(args.markets, args.min_volume) if m.get("cid") and m["closed"]]
    print(f"{len(ms)} markets", flush=True)
    with ThreadPoolExecutor(6) as pool:
        rows = [r for rs in pool.map(one, ms) for r in rs]
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")

    def report(tag, xs):
        if len(xs) < 200:
            return
        w = np.array([x["size"] for x in xs])
        cols = [f"half-spread {np.mean([x['half_spread'] for x in xs]) * 100:+.2f}¢"]
        for dt in HORIZONS:
            v = np.array([x[f"pnl{dt}"] for x in xs]) * 100
            cols.append(f"maker@{dt // 60}m {v.mean():+.2f}¢ ± {v.std() / np.sqrt(len(v)):.2f} (vol-wtd {np.average(v, weights=w):+.2f})")
        print(f"{tag:22s} n={len(xs):6d}  " + "  ".join(cols))

    print(f"{len(rows)} taker trades; maker P&L per share, cents")
    report("all", rows)
    for lo, hi in ((0, 50), (50, 500), (500, 1e12)):
        report(f"trade size {lo}-{hi:g}", [r for r in rows if lo <= r["size"] < hi])
    by = defaultdict(list)
    for r in rows:
        by[datetime.fromtimestamp(r["t"]).year].append(r)
    for y, xs in sorted(by.items()):
        report(f"year {y}", xs)


if __name__ == "__main__":
    main()
