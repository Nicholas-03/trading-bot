"""Can Jev forecast Polymarket questions better than the market price?

    .venv/bin/python scripts/jev_polymarket.py score [--since 2025-01-01]   # ask Jev about each market -> data/jev_polymarket.jsonl
    .venv/bin/python scripts/jev_polymarket.py analyze

Markets: results/polymarket_calibration.jsonl (closed binary markets, YES price 7 days before the scheduled end, outcome).
Jev sees the question, "today's" date (the pricing time) and the end date, twice: blind, and with the market's price.
A model trained after a market resolved may remember the outcome, so results are split by end date: an edge that is
only there for older markets is memory, not forecasting. Trade test: buy the side Jev prefers when it disagrees with
the price by >= 15 points, hold to resolution, return per $ staked (no fees; Polymarket spreads are ~1-5 points).
"""
import json
import sys
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
SRC = ROOT / "results" / "polymarket_calibration.jsonl"
OUT = ROOT / "data" / "jev_polymarket.jsonl"
Q = {"yes": {"type": "noul", "instructions": "Will the prediction-market question `question` resolve YES, i.e. will what it "
                                              "asks happen by `end_date`? Judge as of `today`."}}


def day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def score(since: str = "2024-01-01") -> None:
    import jev_score as js
    rows = [json.loads(line) for line in open(SRC)]
    rows = [r for r in rows if day(r["end"]) >= since]
    done = {r["token"] for r in map(json.loads, open(OUT))} if OUT.exists() else set()
    todo = [r for r in rows if r["token"] not in done]
    print(f"{len(rows)} markets since {since}, {len(todo)} to score", flush=True)
    lock, n = threading.Lock(), 0

    def one(r):
        nonlocal n
        base = {"question": r["q"], "today": day(r["t"]), "end_date": day(r["end"])}
        try:
            blind = js.ask_body({"model": js.MODEL, "state": base, "questions": Q})
            seen = js.ask_body({"model": js.MODEL, "questions": Q,
                                "state": {**base, "market_probability_of_yes": f"{r['p'] * 100:.0f}%"}})
        except Exception as exc:
            print("error", r["q"][:60], exc, flush=True)
            return
        row = {"token": r["token"], "q": r["q"], "end": r["end"], "p": r["p"], "yes": r["yes"], "volume": r["volume"],
               "jev": blind["answers"]["yes"]["noul"], "jev_seen": seen["answers"]["yes"]["noul"]}
        with lock:
            with open(OUT, "a") as f:
                f.write(json.dumps(row) + "\n")
            n += 1
            if n % 500 == 0:
                print(f"  {n}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(one, todo))
    print(f"done: {n}", flush=True)


def analyze() -> None:
    rows = [json.loads(line) for line in open(OUT)]
    periods = defaultdict(list)
    for r in rows:
        d = day(r["end"])
        periods["2024" if d < "2025" else "2025 H1" if d < "2025-07" else "2025 H2" if d < "2026" else
                "2026 H1" if d < "2026-07" else "2026 Jul-Sep"].append(r)
    print(f"{len(rows)} markets. Brier score (lower is better); trade = buy Jev's side when |Jev - price| >= 15 pts")
    print(f"{'period':14s} {'n':>5s} {'market':>7s} {'jev':>7s} {'jev+px':>7s} {'avg':>7s}   {'trades':>6s} {'ret/$':>8s}  (jev+px trades, ret/$)")
    for name in ("2024", "2025 H1", "2025 H2", "2026 H1", "2026 Jul-Sep"):
        rs = periods.get(name) or []
        if not rs:
            continue
        y = np.array([r["yes"] for r in rs], float)
        p, j, js_ = (np.array([r[k] for r in rs]) for k in ("p", "jev", "jev_seen"))
        brier = lambda x: float(np.mean((x - y) ** 2))

        def trades(f):
            ret = []
            for pi, fi, yi in zip(p, f, y):
                if fi - pi >= 0.15:
                    ret.append(yi / pi - 1)
                elif pi - fi >= 0.15:
                    ret.append((1 - yi) / (1 - pi) - 1)
            return len(ret), (float(np.mean(ret)) if ret else 0.0), (float(np.std(ret) / np.sqrt(len(ret))) if ret else 0.0)

        t1, t2 = trades(j), trades(js_)
        print(f"{name:14s} {len(rs):5d} {brier(p):7.4f} {brier(j):7.4f} {brier(js_):7.4f} {brier((p + j) / 2):7.4f}   "
              f"{t1[0]:6d} {t1[1]:+7.1%}±{t1[2]:.1%}  ({t2[0]} {t2[1]:+.1%}±{t2[2]:.1%})")


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    {"score": score, "analyze": analyze}[sys.argv[1]](*sys.argv[2:])
