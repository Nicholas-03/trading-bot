"""Frozen 5-day drift rule (results/frozen_rule_drift5d.json) on the hold-out: pairs the bot's price/liquidity gates skip.

    .venv/bin/python scripts/eval_drift5d_holdout.py
"""
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402
import build_daily_labels as bdl  # noqa: E402

BARS = ROOT / "data" / "daily_bars_holdout.json"


def main():
    load_dotenv(ROOT / ".env")
    ev = {}
    for f in ("laya_alpaca_labels_2021_2023.jsonl", "laya_alpaca_labels_v2.jsonl"):
        for line in open(ROOT / "data" / f):
            r = json.loads(line)
            if r.get("tradable") or r.get("excess_1h") is None:
                continue
            day = datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(bal.ET).date().isoformat()
            k = (r["ticker"], day)
            if k not in ev or r["ts"] < ev[k]["ts"]:  # first news of the stock and day
                ev[k] = {**r, "day": day}
    ev = [r for r in ev.values() if r["excess_1h"] <= -0.10]
    print(len(ev), "hold-out events (first news per stock and day, first hour <= -10%)")
    if BARS.exists():
        bars = json.loads(BARS.read_text())
    else:
        bars = bdl.fetch(sorted({r["ticker"] for r in ev} | {"SPY"}))
        BARS.write_text(json.dumps(bars))
    spy = bars["SPY"]
    days = sorted(spy)
    idx = {d: i for i, d in enumerate(days)}
    out = []
    for r in ev:
        c = bars.get(r["ticker"], {})
        i = idx.get(r["day"])
        if i is None or i + 5 >= len(days) or not c.get(days[i]) or not c.get(days[i + 5]):
            continue
        d0, d5 = days[i], days[i + 5]
        out.append((r, -(c[d5] / c[d0] - spy[d5] / spy[d0]) * 100, r.get("entry", 0)))
    for cost in (0.5, 1.0):
        for name, lo, hi in (("all", 0, 1e9), ("$1-5", 1, 5), ("$5-20", 5, 20), ("$20+ (untradable for volume)", 20, 1e9), ("<$1", 0, 1)):
            xs = [(r, x - cost) for r, x, p in out if lo <= p < hi]
            if len(xs) < 5:
                continue
            v = np.array([x for _, x in xs])
            per_day, years = defaultdict(list), defaultdict(list)
            for r, x in xs:
                per_day[r["day"]].append(x)
                years[r["day"][:4]].append(x)
            dm = np.array([np.mean(x) for x in per_day.values()])
            print(f"cost {cost}% {name:30s} n={len(v):5d} mean {v.mean():+6.2f}% median {np.median(v):+6.2f} "
                  f"day-clustered {dm.mean():+.2f} ± {dm.std() / np.sqrt(len(dm)):.2f} win {np.mean(v > 0):.0%} | "
                  + " ".join(f"{y}:{np.mean(w):+.1f}({len(w)})" for y, w in sorted(years.items())))


if __name__ == "__main__":
    main()
