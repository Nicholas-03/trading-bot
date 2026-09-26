"""Out-of-sample check of the frozen "short after a news drop" rule (results/frozen_rule_drop.json).

    .venv/bin/python scripts/eval_drop_rule.py data/laya_price_paths_2021_2023.jsonl

Short when the price at entry (first 1-minute bar >= news + 60s) is >= 3% below the last close before the news, cover
60 minutes later (flattened by 15:50 ET). One trade per ticker per entry minute (several headlines at once count once).
"""
import json
import sys
from collections import defaultdict

import numpy as np


def main():
    rows = [json.loads(line) for line in open(sys.argv[1])]
    seen, trades = set(), defaultdict(list)
    for r in sorted(rows, key=lambda r: r["ts"]):
        if not r["fwd"].get("60"):
            continue
        key = (r["ticker"], r["ts"][:16])
        if key in seen:
            continue
        seen.add(key)
        for thr in (-0.02, -0.03, -0.04, -0.05):
            if r["early"]["move"] <= thr:
                path = [-r["fwd"][k][0] * 100 for k in ("1", "2", "5", "10", "15", "30") if r["fwd"].get(k)]
                trades[thr].append((r["ts"][:4], -r["fwd"]["60"][0] * 100, r["ticker"], r["ts"][:16], path))
    for thr in (-0.03, -0.02, -0.04, -0.05):
        t = trades[thr]
        g = np.array([x[1] for x in t])
        tag = "FROZEN" if thr == -0.03 else "report"
        print(f"\n[{tag}] move <= {thr:.0%}: n={len(g)}  gross mean {g.mean():+.2f}% median {np.median(g):+.2f}%  "
              f"net@0.10% {g.mean() - 0.10:+.2f}  net@0.30% {g.mean() - 0.30:+.2f} ± {g.std() / np.sqrt(len(g)):.2f}  "
              f"win {np.mean(g > 0.30):.0%}")
        by = defaultdict(list)
        for y, v, *_ in t:
            by[y].append(v)
        print("   by year (gross mean / median / n):",
              {y: f"{np.mean(v):+.2f}/{np.median(v):+.2f}/{len(v)}" for y, v in sorted(by.items())})
        if thr == -0.03:
            short_med = [np.median(v) for v in by.values()]
            ok = (np.median(g) > 0.5, g.mean() - 0.30 > 0, sum(m > 0 for m in short_med) >= 2)
            print("   pass criteria (median gross > 0.5%, mean net@0.30% > 0, positive median in >= 2 years):", ok,
                  "-> PASS" if all(ok) else "-> FAIL")
            print("   worst 5:", [x[:4] for x in sorted(t, key=lambda x: x[1])[:5]])
            # emergency stop, checked only at the +1/2/5/10/15/30 minute closes (a real stop fills near its level)
            for stop in (5, 10, 20):
                s = np.array([next((p for p in x[4] if p <= -stop), x[1]) for x in t])
                print(f"   stop {stop:2d}%: mean {s.mean():+.2f}% median {np.median(s):+.2f}%  "
                      f"net@0.30% {s.mean() - 0.30:+.2f} ± {s.std() / np.sqrt(len(s)):.2f}  worst {s.min():+.1f}%  "
                      f"stopped {np.mean(s != np.array([x[1] for x in t])):.0%}")


if __name__ == "__main__":
    main()
