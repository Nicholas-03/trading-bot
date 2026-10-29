"""Can bad stretches of the liquid_fade book be seen coming? Per-trade results split by things known on entry day.

    .venv/bin/python scripts/eval_fade_regime.py   # data/laya_fade_labels.jsonl (2021-26) + SPY daily bars -> stdout

Trades as in scripts/eval_fade_risk.py (first news per ticker-day, news-day move >= 10% vs SPY, short at the close for
5 days, SPY-hedged, after COST). Each predictor is bucketed and shown for 2021-23 (A) and 2024-26 (B): a usable
predictor has to rank the buckets the same way in both periods.
- busy: number of signals that day (earnings season brings 20+)
- season: week of the calendar quarter (weeks 3-7 hold most earnings reports)
- trailing: mean result of the trades entered 6-25 trading days before (their 5-day outcome is known by entry day)
- spy_20d / vol_20d: SPY's return and daily volatility over the 20 trading days before entry
"""
import statistics as st
import sys
from collections import defaultdict
from datetime import date
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402
from eval_fade_risk import COST, load  # noqa: E402


def spy_closes() -> dict[str, float]:
    bars = bal.get("/v2/stocks/bars", {"symbols": "SPY", "timeframe": "1Day", "start": "2020-10-01", "end": "2026-10-28",
                                       "feed": "sip", "adjustment": "all", "limit": 10000}).get("bars", {}).get("SPY", [])
    return {b["t"][:10]: b["c"] for b in bars}


def line(name: str, xs: list[float]) -> str:
    if len(xs) < 50:
        return f"{name:>14s} n={len(xs)}"
    m, s = st.mean(xs), st.stdev(xs)
    return f"{name:>14s} n={len(xs):5d} mean {m * 100:+5.2f}% (t {m / s * len(xs) ** 0.5:4.1f}) win {sum(x > 0 for x in xs) / len(xs):3.0%}"


def main():
    load_dotenv(ROOT / ".env")
    ev = load()
    for r in ev:
        r["ret"] = -r["d5_excess"] - COST
    closes = spy_closes()
    days = sorted(closes)
    pos = {d: i for i, d in enumerate(days)}
    by_day = defaultdict(list)
    for r in ev:
        by_day[r["day"]].append(r["ret"])
    for r in ev:
        d = r["day"]
        r["busy"] = len(by_day[d])
        dd = date.fromisoformat(d)
        r["week_q"] = ((dd.month - 1) % 3 * 31 + dd.day) // 7 + 1
        i = pos.get(d)
        if i is None or i < 26:
            continue
        past = [x for k in days[i - 25:i - 5] for x in by_day.get(k, [])]
        r["trailing"] = st.mean(past) if len(past) >= 20 else None
        r["spy_20d"] = closes[d] / closes[days[i - 20]] - 1
        rets = [closes[days[j]] / closes[days[j - 1]] - 1 for j in range(i - 19, i + 1)]
        r["vol_20d"] = st.pstdev(rets)
    preds = {
        "busy": [("1-5", lambda v: v <= 5), ("6-10", lambda v: 6 <= v <= 10), ("11-20", lambda v: 11 <= v <= 20), ("21+", lambda v: v > 20)],
        "week_q": [("weeks 1-2", lambda v: v <= 2), ("weeks 3-7", lambda v: 3 <= v <= 7), ("weeks 8-14", lambda v: v >= 8)],
        "trailing": [("< 0", lambda v: v < 0), ("0 to +2%", lambda v: 0 <= v < 0.02), (">= +2%", lambda v: v >= 0.02)],
        "spy_20d": [("< -3%", lambda v: v < -0.03), ("-3..+3%", lambda v: -0.03 <= v <= 0.03), ("> +3%", lambda v: v > 0.03)],
        "vol_20d": [("< 0.8%/day", lambda v: v < 0.008), ("0.8-1.3%", lambda v: 0.008 <= v < 0.013), (">= 1.3%", lambda v: v >= 0.013)],
    }
    for p, buckets in preds.items():
        print(f"\n== {p}")
        for period, lo, hi in (("A 2021-23", "2021", "2023"), ("B 2024-26", "2024", "2026")):
            sel = [r for r in ev if lo <= r["day"][:4] <= hi and r.get(p) is not None]
            print(f"  {period}")
            for name, f in buckets:
                print("   " + line(name, [r["ret"] for r in sel if f(r[p])]))


if __name__ == "__main__":
    main()
