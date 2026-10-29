"""Do Polymarket prices keep moving after a news jump? (is there time for a news reader like Laya to trade?)

    .venv/bin/python scripts/polymarket_jumps.py [--markets 3000] [--jump 0.10]

For closed binary markets: 1-minute price history over the last 14 days before close. A jump is a move of at least `jump`
within 5 minutes starting from a price between 5% and 95%. We "trade" in the jump's direction one minute after it is
visible (a bot seeing the same news would be about that late) and measure the signed change after 5, 15 and 60 minutes and
at resolution. Positive = the market under-reacts and a fast reader earns it; ~0 = the market is already done.
Costs: prices are last-trade/midpoint, so 1 and 2 cents are charged for crossing the spread.
"""
import argparse
import json
import sys
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import polymarket_calibration as pc  # noqa: E402  (market listing and HTTP helper)

WEEK = 7 * 86400


def history(token: str, end: float) -> list[tuple[int, float]]:
    pts = {}
    for k in (2, 1):  # two 7-day windows (the API's limit at 1-minute fidelity)
        d = pc.get(f"{pc.CLOB}/prices-history", {"market": token, "startTs": int(end - k * WEEK),
                                                 "endTs": int(end - (k - 1) * WEEK), "fidelity": 1})
        for x in (d or {}).get("history") or []:
            pts[int(x["t"]) // 60 * 60] = float(x["p"])
    return sorted(pts.items())


def jumps(m: dict, jump: float) -> list[dict]:
    h = history(m["token"], m["closed"])
    if len(h) < 120:
        return []
    t = np.array([x[0] for x in h])
    p = np.array([x[1] for x in h])
    at = lambda s: p[max(0, np.searchsorted(t, s, side="right") - 1)]  # noqa: E731  last price at or before s
    out, skip_until = [], 0
    for i in range(len(t)):
        if t[i] < skip_until or t[i] - t[0] < 300:
            continue
        before = at(t[i] - 300)
        move = p[i] - before
        if abs(move) < jump or not 0.05 <= before <= 0.95:
            continue
        side = 1 if move > 0 else -1
        e_t = t[i] + 60
        if e_t >= t[-1]:
            break
        entry = at(e_t)
        res = 1.0 if m["yes"] else 0.0
        out.append({"q": m["q"], "t": int(t[i]), "before": round(before, 3), "jump": round(move, 3), "entry": entry,
                    **{f"d{k}": round(side * (at(e_t + k * 60) - entry), 4) for k in (5, 15, 60)},
                    "d_res": round(side * (res - entry), 4), "to_close_h": round((m["closed"] - e_t) / 3600, 1)})
        skip_until = t[i] + 3600
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=3000)
    ap.add_argument("--min-volume", type=float, default=50_000)
    ap.add_argument("--jump", type=float, default=0.10)
    ap.add_argument("--out", default=str(ROOT / "results" / "polymarket_jumps.jsonl"))
    args = ap.parse_args()
    ms = [m for m in pc.markets(args.markets, args.min_volume) if m["closed"]]
    print(f"{len(ms)} markets", flush=True)
    t0 = time.time()
    with ThreadPoolExecutor(6) as pool:
        ev = [e for es in pool.map(lambda m: jumps(m, args.jump), ms) for e in es]
    print(f"{len(ev)} jumps in {time.time() - t0:.0f}s")
    with open(args.out, "w") as f:
        for e in ev:
            f.write(json.dumps(e) + "\n")

    def report(tag, xs):
        if len(xs) < 20:
            return
        cols = []
        for k in ("d5", "d15", "d60", "d_res"):
            v = np.array([x[k] for x in xs]) * 100
            cols.append(f"{k}: {v.mean():+5.2f}¢ ± {v.std() / np.sqrt(len(v)):.2f}")
        print(f"{tag:28s} n={len(xs):5d}  " + "  ".join(cols))

    print("signed change in the jump's direction, from 1 minute after the jump (cents per share; spread cost 1-2¢ not taken off)")
    report("all", ev)
    for lo, hi in ((0.10, 0.20), (0.20, 0.35), (0.35, 1.0)):
        report(f"jump {lo:.2f}-{hi:.2f}", [e for e in ev if lo <= abs(e["jump"]) < hi])
    for lo, hi in ((0, 6), (6, 48), (48, 1e9)):
        report(f"closes in {lo}-{hi}h", [e for e in ev if lo <= e["to_close_h"] < hi])
    report("up jumps", [e for e in ev if e["jump"] > 0])
    report("down jumps", [e for e in ev if e["jump"] < 0])
    by = defaultdict(list)
    for e in ev:
        by[datetime.fromtimestamp(e["t"]).year].append(e)
    for y, xs in sorted(by.items()):
        report(f"year {y}", xs)


if __name__ == "__main__":
    main()
