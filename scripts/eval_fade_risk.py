"""Loss reduction for the liquid_fade book: which simple rules cut losses and drawdowns without killing the edge?

    .venv/bin/python scripts/eval_fade_risk.py   # data/laya_fade_labels.jsonl (2021-26) -> stdout

Events: first news per (ticker, day) on tradable stocks whose news day moved >= 10% against SPY either way (the live
book's gate); the trade is a short at that close held 5 days (-d5_ret, or -d5_excess with a SPY hedge), minus COST.
Rules are judged on 2021-23 (A) and checked on 2024-26 (B) so a rule picked for A has to hold up on B.

Portfolio: 5 sleeves of equal capital, one per entry day of the 5-day hold; each day's sleeve is split equally over
that day's shorts (at most MAX_PER_DAY), so the account return of a day's cohort is mean(trade) / 5. Drawdown is
measured on that cohort series in entry order (an approximation: the real path between entry and exit is not in the
labels).
"""
import json
import statistics as st
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
COST = 0.002  # per round trip, as in the backtest of the live book
MIN_MOVE = 0.10


def load() -> list[dict]:
    first = {}
    for line in open(ROOT / "data" / "laya_fade_labels.jsonl"):
        r = json.loads(line)
        if abs(r["day_excess"]) < MIN_MOVE:
            continue
        k = (r["ticker"], r["ts"][:10])
        if k not in first or r["ts"] < first[k]["ts"]:
            first[k] = r
    ev = sorted(first.values(), key=lambda r: r["ts"])
    heads = defaultdict(int)  # same headline on the same day = one news story moving several stocks
    for r in ev:
        heads[(r["ts"][:10], r["headline"])] += 1
    for r in ev:
        r["day"] = r["ts"][:10]
        r["cluster"] = heads[(r["day"], r["headline"])]
    return ev


def stats(trades: list[float]) -> str:
    if len(trades) < 30:
        return f"n={len(trades)}"
    m, s = st.mean(trades), st.stdev(trades)
    q = sorted(trades)
    return (f"n={len(trades):5d} mean {m * 100:+5.2f}% t {m / s * len(trades) ** 0.5:4.1f} win {sum(x > 0 for x in trades) / len(trades):3.0%}"
            f" p5 {q[len(q) // 20] * 100:+6.1f}% worst {q[0] * 100:+6.0f}%")


def portfolio(ev: list[dict], ret, max_per_day: int | None) -> str:
    by_day = defaultdict(list)
    for r in ev:
        by_day[r["day"]].append(ret(r))
    eq, peak, dd, months = 1.0, 1.0, 0.0, defaultdict(float)
    for d in sorted(by_day):
        xs = by_day[d][:max_per_day] if max_per_day else by_day[d]
        g = st.mean(xs) / 5
        eq *= 1 + g
        peak = max(peak, eq)
        dd = min(dd, eq / peak - 1)
        months[d[:7]] += g
    years = len({d[:4] for d in by_day}) or 1
    return (f"| per yr {(eq ** (1 / years) - 1) * 100:+5.1f}% maxDD {dd * 100:5.1f}% worst month {min(months.values()) * 100:+5.1f}%"
            f" losing months {sum(v < 0 for v in months.values())}/{len(months)}")


def main():
    ev = load()
    short = lambda r: -r["d5_ret"] - COST  # noqa: E731
    hedged = lambda r: -r["d5_excess"] - COST  # noqa: E731
    rules = {
        "all (live book)": lambda r: True,
        "drops only": lambda r: r["day_excess"] < 0,
        "pops only": lambda r: r["day_excess"] > 0,
        "move < 30%": lambda r: abs(r["day_excess"]) < 0.30,
        "move < 50%": lambda r: abs(r["day_excess"]) < 0.50,
        "single-ticker news": lambda r: r["n_tickers"] == 1,
        "not a shared headline": lambda r: r["cluster"] == 1,
        "pops < 30% + all drops": lambda r: r["day_excess"] < 0 or r["day_excess"] < 0.30,
    }
    for label, ret in (("UNHEDGED short", short), ("SPY-HEDGED short", hedged)):
        print(f"\n===== {label} (after {COST * 100:.1f}% cost)")
        for name, keep in rules.items():
            for period, lo, hi in (("A 2021-23", "2021", "2023"), ("B 2024-26", "2024", "2026")):
                sel = [r for r in ev if keep(r) and lo <= r["day"][:4] <= hi]
                print(f"{name:24s} {period}  {stats([ret(r) for r in sel])}  {portfolio(sel, ret, None)}")
    print("\n===== at most N shorts per day (first by news time), unhedged")
    for n in (None, 10, 5, 3):
        for period, lo, hi in (("A 2021-23", "2021", "2023"), ("B 2024-26", "2024", "2026")):
            sel = [r for r in ev if lo <= r["day"][:4] <= hi]
            print(f"max/day {str(n):5s} {period}  {portfolio(sel, short, n)}")


if __name__ == "__main__":
    main()
