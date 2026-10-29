"""Short after "Shares Halted On Circuit Breaker To The Upside" news, with a stop, on real 1-minute bars.

    .venv/bin/python scripts/eval_halt_fade.py [--paths data/halt_up_paths.jsonl]

For the first upside-halt headline of each stock and day (data/halt_up_paths.jsonl, built with
build_price_paths.py --all): short at the open of the first bar at least `wait` minutes after the bot's entry bar
(entry bar = first bar >= news + 60 s, i.e. after the halt has lifted), cover at the close 60 minutes after that
entry bar (or 15:50 ET), or earlier when a bar's high reaches the stop (filled at the stop, or at the bar's open if it
gapped through). P&L per trade = short return minus the round-trip cost. Clustered by day for the standard error.
"""
import argparse
import json
import sys
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import build_alpaca_labels as bal  # noqa: E402
import build_price_paths as bpp  # noqa: E402

CACHE = ROOT / "data" / "halt_bars"
WAITS = (0, 5, 10)
STOPS = (0.10, 0.20, 0.30, None)
COSTS = (0.5, 1.0)


def day_bars(day: str, tickers: list[str]) -> dict[str, list]:
    """Cached 1-minute bars per ticker as [t, o, h, l, c] rows."""
    out = CACHE / f"{day}.json"
    if out.exists():
        return json.loads(out.read_text())
    bars = bpp.fetch_day_bars(date.fromisoformat(day), tickers)
    rows = {t: [[k, *v[:4]] for k, v in sorted(b.items())] for t, b in bars.items()}
    out.write_text(json.dumps(rows))
    return rows


def trade(bars: list, news_ts: int, flatten: int, wait: int, stop: float | None) -> float | None:
    t = [b[0] for b in bars]
    i0 = next((i for i, x in enumerate(t) if x >= news_ts + 60), None)  # the bot's entry bar (after the halt)
    if i0 is None:
        return None
    start = t[i0] + wait * 60
    i = next((k for k in range(i0, len(t)) if t[k] >= start), None)
    if i is None or t[i] >= flatten:
        return None
    entry = bars[i][1]
    end = min(t[i0] + 3600, flatten)
    last = entry
    for k in range(i, len(t)):
        if t[k] >= end:
            break
        o, h, c = bars[k][1], bars[k][2], bars[k][4]
        if stop is not None and h >= entry * (1 + stop):
            fill = max(entry * (1 + stop), o) if k > i else entry * (1 + stop)
            return -(fill / entry - 1) * 100
        last = c
    return -(last / entry - 1) * 100


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--paths", default=str(ROOT / "data" / "halt_up_paths.jsonl"))
    args = ap.parse_args()
    CACHE.mkdir(parents=True, exist_ok=True)
    ev, seen = [], set()
    for r in sorted((json.loads(l) for l in open(args.paths)), key=lambda r: r["ts"]):
        if (r["ticker"], r["ts"][:10]) not in seen:
            seen.add((r["ticker"], r["ts"][:10]))
            ev.append(r)
    by_day = defaultdict(list)
    for r in ev:
        by_day[datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).astimezone(bal.ET).date().isoformat()].append(r)
    res = defaultdict(list)  # (wait, stop) -> [(day, entry price, gross %)]
    for n, (day, rs) in enumerate(sorted(by_day.items()), 1):
        bars = day_bars(day, sorted({r["ticker"] for r in rs}))
        d = date.fromisoformat(day)
        flatten = int(datetime(d.year, d.month, d.day, 15, 50, tzinfo=bal.ET).timestamp())
        for r in rs:
            b = bars.get(r["ticker"])
            if not b:
                continue
            ts = int(datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).timestamp())
            for w in WAITS:
                for s in STOPS:
                    x = trade(b, ts, flatten, w, s)
                    if x is not None:
                        res[(w, s)].append((day, r["entry"], x))
        if n % 100 == 0:
            print(f"{n}/{len(by_day)} days", flush=True)
    for (w, s), xs in sorted(res.items(), key=lambda kv: (kv[0][0], kv[0][1] or 9)):
        for cost in COSTS:
            v = np.array([x for _, _, x in xs]) - cost
            per_day = defaultdict(list)
            for (day, _, _), y in zip(xs, v):
                per_day[day].append(y)
            dm = np.array([sum(y) for y in per_day.values()])  # P&L per day in trade units
            se = dm.std() * np.sqrt(len(dm)) / len(v)
            years = defaultdict(list)
            for (day, _, _), y in zip(xs, v):
                years[day[:4]].append(y)
            print(f"wait {w:2d}m stop {('%d%%' % (s * 100)) if s else 'none':>4} cost {cost:.1f}%: n={len(v)} mean {v.mean():+.2f}% ± {se:.2f} "
                  f"median {np.median(v):+.2f} win {np.mean(v > 0):.0%} worst {v.min():+.0f} | "
                  + " ".join(f"{y}:{np.mean(z):+.1f}" for y, z in sorted(years.items())))


if __name__ == "__main__":
    main()
