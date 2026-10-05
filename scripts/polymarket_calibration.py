"""Are Polymarket prices calibrated? (favourite-longshot bias test on closed binary markets)

    .venv/bin/python scripts/polymarket_calibration.py [--markets 3000] [--days-before 7]

For each closed market with enough volume: the YES price `days_before` days before its scheduled end (only if the market
was still open then and not already at 0/1), and how it resolved. If long shots are overpriced, buying NO on them and
holding to resolution earns more than it costs. Public APIs, no key: gamma-api (markets) and clob (price history).
"""
import argparse
import json
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
GAMMA, CLOB = "https://gamma-api.polymarket.com", "https://clob.polymarket.com"
BUCKETS = (0, 0.02, 0.05, 0.10, 0.20, 0.35, 0.50, 0.65, 0.80, 0.90, 0.95, 0.98, 1.0001)


def get(url: str, params: dict):
    for attempt in range(5):
        try:
            req = urllib.request.Request(f"{url}?{urllib.parse.urlencode(params)}", headers={"User-Agent": "research"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except Exception:
            time.sleep(2 ** attempt)
    return None


def ts(s: str | None) -> float | None:
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() if s else None


def markets(n: int, min_volume: float) -> list[dict]:
    """Highest-volume markets per quarter of scheduled end (the API serves 100 per page and offsets below 5000)."""
    quarters = [f"{y}-{m:02d}-01" for y in range(2023, 2027) for m in (1, 4, 7, 10)]
    quarters = [q for q in quarters if "2023-07-01" <= q <= "2026-10-01"]
    out = []
    for lo, hi in zip(quarters, quarters[1:]):
        offset, taken = 0, 0
        while taken < n // (len(quarters) - 1) and offset < 4900:
            page = get(f"{GAMMA}/markets", {"closed": "true", "limit": 100, "offset": offset, "order": "volumeNum",
                                            "ascending": "false", "end_date_min": lo, "end_date_max": hi})
            if not page or not isinstance(page, list):
                break
            offset += len(page)
            for m in page:
                try:
                    prices = [float(p) for p in json.loads(m["outcomePrices"])]
                    outcomes = json.loads(m["outcomes"])
                except (KeyError, TypeError, ValueError):
                    continue
                if outcomes != ["Yes", "No"] or sorted(prices) != [0.0, 1.0] or float(m.get("volumeNum") or 0) < min_volume:
                    continue  # binary, cleanly resolved, liquid enough
                out.append({"q": m["question"], "yes": prices[0] == 1.0, "token": json.loads(m["clobTokenIds"])[0],
                            "end": ts(m.get("endDate")), "closed": ts(m.get("closedTime")) or ts(m.get("endDate")),
                            "volume": float(m["volumeNum"]), "cid": m.get("conditionId")})
                taken += 1
    return out


def price_at(m: dict, t: float) -> float | None:
    d = get(f"{CLOB}/prices-history", {"market": m["token"], "startTs": int(t - 6 * 3600), "endTs": int(t), "fidelity": 60})
    h = (d or {}).get("history") or []
    return h[-1]["p"] if h else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--markets", type=int, default=3000)
    ap.add_argument("--min-volume", type=float, default=50_000)
    ap.add_argument("--days-before", type=float, default=7)
    ap.add_argument("--out", default=str(ROOT / "results" / "polymarket_calibration.jsonl"))
    args = ap.parse_args()
    ms = markets(args.markets, args.min_volume)
    print(f"{len(ms)} closed binary markets with volume >= ${args.min_volume:,.0f}", flush=True)

    def one(m):
        if not m["end"] or not m["closed"]:
            return None
        t = m["end"] - args.days_before * 86400
        if m["closed"] <= t:  # already resolved by then
            return None
        p = price_at(m, t)
        return {**m, "p": p, "t": t} if p is not None and 0.001 < p < 0.999 else None

    with ThreadPoolExecutor(8) as pool:
        rows = [r for r in pool.map(one, ms) if r]
    Path(args.out).parent.mkdir(exist_ok=True)
    with open(args.out, "w") as f:
        for r in rows:
            f.write(json.dumps(r) + "\n")
    print(f"{len(rows)} priced {args.days_before:g} days before the scheduled end\n")
    print(f"{'YES price':>11} {'n':>5} {'avg price':>9} {'YES rate':>8}   buy-YES return   buy-NO return  (per $ staked, to resolution)")
    for lo, hi in zip(BUCKETS, BUCKETS[1:]):
        x = [r for r in rows if lo <= r["p"] < hi]
        if len(x) < 10:
            continue
        p = np.array([r["p"] for r in x])
        y = np.array([r["yes"] for r in x], float)
        ry, rn = y / p - 1, (1 - y) / (1 - p) - 1
        print(f"{lo:5.2f}-{hi:4.2f} {len(x):5d} {p.mean():9.3f} {y.mean():8.3f}   {ry.mean():+7.1%} ± {ry.std() / np.sqrt(len(x)):5.1%}"
              f"   {rn.mean():+7.1%} ± {rn.std() / np.sqrt(len(x)):5.1%}")
    by = defaultdict(list)
    for r in rows:
        if r["p"] < 0.10:
            by[datetime.fromtimestamp(r["t"]).year].append((1 - r["yes"]) / (1 - r["p"]) - 1)
    print("\nbuy NO when YES < 10%, by year:", {y: f"{np.mean(v):+.1%} n={len(v)}" for y, v in sorted(by.items())})


if __name__ == "__main__":
    main()
