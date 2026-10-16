"""Jev (zero-shot) vs Laya (base and fine-tuned) on the same test pairs.

    .venv/bin/python scripts/jev_compare.py [--laya kaggle/laya-trading/results/xs_big/test_predictions.jsonl] [--cost 0.2]

For each signal (Laya: P(buy) - P(short); Jev: P(up) - P(down) per horizon, and the impact score) on the pairs both
models scored: the rank correlation with the SPY-hedged return over the first hour and over 5 days (entry at the news
day's close), and a trade test: take the q% of pairs with the strongest signal, trade in its direction, and report the
mean return per trade after `cost` % round trip, with a t-stat clustered by day. Returns from data/laya_daily_labels.jsonl.
"""
import argparse
import json
import math
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def rank(x):
    order = sorted(range(len(x)), key=lambda i: x[i])
    r = [0.0] * len(x)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and x[order[j + 1]] == x[order[i]]:
            j += 1
        for k in range(i, j + 1):
            r[order[k]] = (i + j) / 2
        i = j + 1
    return r


def spearman(a, b):
    ra, rb = rank(a), rank(b)
    n = len(a)
    ma, mb = sum(ra) / n, sum(rb) / n
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va = sum((x - ma) ** 2 for x in ra)
    vb = sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va and vb else 0.0


def day_t(pnl, days):
    """Mean and t-stat of per-trade P&L with trades on the same day averaged first (news clusters by day)."""
    by = defaultdict(list)
    for p, d in zip(pnl, days):
        by[d].append(p)
    m = [sum(v) / len(v) for v in by.values()]
    if len(m) < 3:
        return sum(pnl) / max(len(pnl), 1), 0.0
    mu = sum(m) / len(m)
    sd = math.sqrt(sum((x - mu) ** 2 for x in m) / (len(m) - 1))
    return sum(pnl) / len(pnl), mu / (sd / math.sqrt(len(m))) if sd else 0.0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--laya", default=str(ROOT / "kaggle/laya-trading/results/xs_big/test_predictions.jsonl"))
    ap.add_argument("--cost", type=float, default=0.2, help="round trip, %")
    args = ap.parse_args()
    laya = defaultdict(dict)
    for line in open(args.laya):
        r = json.loads(line)
        p = r["probs"]
        if all(isinstance(v, float) and v == v for v in p.values()):
            laya[(r["news_id"], r["ticker"])][r["model"]] = p["buy"] - p["short"]
    jev = {(r["news_id"], r["ticker"]): r for r in map(json.loads, open(ROOT / "data" / "jev_scores.jsonl"))}
    keys = {k for k in laya if k in jev and len(laya[k]) == 2}
    ret = {}
    for line in open(ROOT / "data" / "laya_daily_labels.jsonl"):
        r = json.loads(line)
        k = (r["news_id"], r["ticker"])
        if k in keys:
            ret[k] = (r["excess_1h"], r.get("d5_excess"), r["ts"][:10])
    keys = sorted(k for k in keys if k in ret)
    print(f"{len(keys)} pairs scored by both, {min(ret[k][2] for k in keys)} .. {max(ret[k][2] for k in keys)}, "
          f"cost {args.cost}% per trade\n")
    signals = {
        "laya base": lambda k: laya[k]["laya_base"],
        "laya fine-tuned": lambda k: laya[k]["laya_ft"],
        "jev 1h up-down": lambda k: jev[k]["p_up_1h"] - jev[k]["p_down_1h"],
        "jev 5d up-down": lambda k: jev[k]["p_up_5d"] - jev[k]["p_down_5d"],
        "jev impact": lambda k: jev[k]["impact"] - 2,
        "jev impact x subject": lambda k: (jev[k]["impact"] - 2) * jev[k]["subject"],
    }
    for h, idx in (("first hour", 0), ("5 days from the close", 1)):
        ks = [k for k in keys if ret[k][idx] is not None and abs(ret[k][idx]) < 1]
        y = [ret[k][idx] for k in ks]
        days = [ret[k][2] for k in ks]
        print(f"== {h}: n={len(ks)}")
        print(f"{'signal':24s} {'rank corr':>9s} " + " ".join(f"{'top ' + str(q) + '%':>22s}" for q in (10, 2, 0.5)))
        for name, f in signals.items():
            s = [f(k) for k in ks]
            cells = []
            for q in (10, 2, 0.5):
                top = sorted(range(len(ks)), key=lambda i: -abs(s[i]))[: max(1, int(len(ks) * q / 100))]
                pnl = [(1 if s[i] > 0 else -1) * y[i] * 100 - args.cost for i in top]
                mu, t = day_t(pnl, [days[i] for i in top])
                cells.append(f"{mu:+7.2f}% t{t:+5.1f} n{len(top):5d}")
            print(f"{name:24s} {spearman(s, y):+9.3f} " + " ".join(f"{c:>22s}" for c in cells))
        print()


if __name__ == "__main__":
    main()
