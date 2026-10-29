"""What would a NEW market maker earn on Polymarket? Conservative back-of-queue simulation on PMXT order-book hours.

    .venv/bin/python scripts/polymarket_mm_queue.py 2026-03-01T12 2026-03-05T18 ...

PMXT archives every order-book change (best bid/ask on each update) as hourly Parquet at r2.pmxt.dev. For each active
YES book (>= 200 updates in the hour, spread <= 3 cents, price 5-95%) we always rest one order at the current best bid
and one at the best ask, joining the back of the queue; if someone improves the price we move up (back of the new
queue). We only count a fill when our whole price level is gone (the best bid drops below our bid / best ask rises
above our ask) AND a public taker trade hit our price within 5 seconds (a level that vanished by cancellation leaves us
resting at the front instead; we are then filled by the next taker trade at our price). These are the fills a newcomer
at the back of the queue certainly gets, and the most adverse ones.
Maker P&L per share = mid 1/5/30 minutes after the fill minus our buy price (or our sell price minus that mid).
Each hour file (~600 MB) is downloaded, processed and deleted.
"""
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import numpy as np
import pyarrow.parquet as pq

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import polymarket_calibration as pc  # noqa: E402  (HTTP helper)

DATA = "https://data-api.polymarket.com"
TMP = ROOT / "data" / "pmxt"
HORIZONS = (60, 300, 1800)


def books(path: Path) -> tuple[dict[str, list[tuple[float, float, float]]], dict[str, str]]:
    """token -> [(time, best_bid, best_ask)] for YES tokens, time-ordered; token -> market (condition) id."""
    out, cid = defaultdict(list), {}
    f = pq.ParquetFile(path)
    for g in range(f.num_row_groups):
        for r in f.read_row_group(g, columns=["data"]).column(0).to_pylist():
            d = json.loads(r)
            if d.get("side") == "YES" and d.get("best_bid") and d.get("best_ask"):
                out[d["token_id"]].append((float(d["timestamp"]), float(d["best_bid"]), float(d["best_ask"])))
                cid[d["token_id"]] = d["market_id"]
    for v in out.values():
        v.sort()
    return out, cid


def taker_trades(cid: str, token: str, start: float) -> list[tuple[float, int, float]]:
    """(time, taker sign on YES, YES price) for the hour, from the public data API."""
    out, offset = [], 0
    while offset < 3000:
        page = pc.get(f"{DATA}/trades", {"market": cid, "start": int(start), "end": int(start) + 3600 + 1800,
                                         "limit": 500, "offset": offset})
        if not isinstance(page, list) or not page:
            break
        for x in page:
            yes = x.get("asset") == token
            buy = x.get("side") == "BUY"
            out.append((float(x["timestamp"]), 1 if buy == yes else -1,
                        float(x["price"]) if yes else 1 - float(x["price"])))
        offset += len(page)
        if len(page) < 500:
            break
    return sorted(out)


def simulate(series: list[tuple[float, float, float]], trades: list[tuple[float, int, float]]) -> list[dict]:
    t = np.array([x[0] for x in series])
    mids = np.array([(x[1] + x[2]) / 2 for x in series])
    mid_at = lambda T: mids[np.searchsorted(t, T, side="right") - 1]  # noqa: E731
    tr_t = np.array([x[0] for x in trades])

    def hit(t0, t1, sign, price):  # a taker trade of `sign` at or through `price` in [t0, t1]
        i, j = np.searchsorted(tr_t, t0), np.searchsorted(tr_t, t1, side="right")
        return any(trades[k][1] == sign and sign * (trades[k][2] - price) <= 1e-9 for k in range(i, j))

    fills, bid, ask, wait_bid, wait_ask = [], None, None, 0.0, 0.0
    front_bid = front_ask = False  # our level emptied by cancellations: we are alone at the front
    prev_t = series[0][0]
    for tt, bb, ba in series:
        spread_ok = 0 < ba - bb <= 0.03 and 0.05 <= (bb + ba) / 2 <= 0.95
        if bid is not None and (bb < bid - 1e-9 or front_bid):
            if hit(prev_t - 5, tt + 1, -1, bid):
                fills.append((tt, 1, bid))
                bid, wait_bid, front_bid = None, tt + 1, False
            elif bb < bid - 1e-9:
                front_bid = True
        if ask is not None and (ba > ask + 1e-9 or front_ask):
            if hit(prev_t - 5, tt + 1, 1, ask):
                fills.append((tt, -1, ask))
                ask, wait_ask, front_ask = None, tt + 1, False
            elif ba > ask + 1e-9:
                front_ask = True
        if bid is None or bb > bid + 1e-9:
            bid, front_bid = (bb if spread_ok and tt >= wait_bid else None), False
        if ask is None or ba < ask - 1e-9:
            ask, front_ask = (ba if spread_ok and tt >= wait_ask else None), False
        prev_t = tt
    out = []
    for tt, side, p in fills:
        if tt + max(HORIZONS) > t[-1]:
            continue
        out.append({"t": tt, "side": side, "edge0": side * (mid_at(tt - 1e-6) - p),
                    **{f"pnl{h}": side * (mid_at(tt + h) - p) for h in HORIZONS}})
    return out


def main():
    TMP.mkdir(parents=True, exist_ok=True)
    allf = []
    for hour in sys.argv[1:]:
        path = TMP / f"polymarket_orderbook_{hour}.parquet"
        if not path.exists():
            subprocess.run(["curl", "-s", "-m", "900", "-o", str(path),
                            f"https://r2.pmxt.dev/polymarket_orderbook_{hour}.parquet"], check=True)
        bk, cid = books(path)
        path.unlink()
        start = datetime.strptime(hour, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc).timestamp()
        active = [tok for tok, s in bk.items() if len(s) >= 200 and any(0 < a - b <= 0.03 and 0.05 <= (a + b) / 2 <= 0.95 for _, b, a in s)]

        def run(tok):
            return [{**f, "hour": hour, "token": tok} for f in simulate(bk[tok], taker_trades(cid[tok], tok, start))]

        with ThreadPoolExecutor(6) as pool:
            fills = [f for fs in pool.map(run, active) for f in fs]
        allf += fills
        v = np.array([f["pnl300"] for f in fills]) * 100
        print(f"{hour}: {sum(len(s) >= 200 for s in bk.values())} active books, {len(fills)} fills, "
              f"maker@5m {v.mean():+.3f}¢ ± {v.std() / np.sqrt(max(1, len(v))):.3f}", flush=True)
    with open(ROOT / "results" / "polymarket_mm_queue.jsonl", "w") as f:
        for x in allf:
            f.write(json.dumps(x) + "\n")
    print(f"\n{len(allf)} fills over {len(sys.argv) - 1} hours; per share, cents")
    for h in HORIZONS:
        v = np.array([x[f"pnl{h}"] for x in allf]) * 100
        per_tok = defaultdict(list)
        for x, y in zip(allf, v):
            per_tok[x["token"]].append(y)
        tm = np.array([np.mean(y) for y in per_tok.values()])
        print(f"  maker@{h // 60:2d}m: mean {v.mean():+.3f}¢ ± {v.std() / np.sqrt(len(v)):.3f}  median {np.median(v):+.2f}¢  "
              f"(market-averaged {tm.mean():+.3f}¢ ± {tm.std() / np.sqrt(len(tm)):.3f}, {len(tm)} books)")
    print(f"  spread captured at fill vs prior mid: {np.mean([x['edge0'] for x in allf]) * 100:+.3f}¢")


if __name__ == "__main__":
    main()
