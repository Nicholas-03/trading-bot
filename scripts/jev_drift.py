"""Can Jev's reading of the news pick which first-hour crashes (or pops) keep drifting over 5 days?

    .venv/bin/python scripts/jev_drift.py score      # ask Jev about every drift event with |first hour| >= 10%
    .venv/bin/python scripts/jev_drift.py analyze    # 5-day short P&L by Jev's answers; rules picked on 2021-2024, tested on 2025-26

Events and P&L come from scripts/study_drift.py (short at the news day's close, cover 5 trading days later, SPY-hedged,
0.5% cost, entry >= $1, split errors removed). The state gives Jev the ticker, the headline and summary, and the
stock's first-hour move against the market; no date. Answers go to data/jev_drift_scores.jsonl (resumable).
"""
import json
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
OUT = ROOT / "data" / "jev_drift_scores.jsonl"

CATEGORIES = {
    "earnings": "Quarterly or annual results, guidance or outlook",
    "financing": "Stock offering, private placement, warrants, convertible notes, shelf registration or other dilution",
    "clinical_regulatory": "Clinical trial results, FDA or other regulator decision on a product",
    "analyst": "Analyst rating or price target change",
    "trading_halt": "Trading halt, resumption, circuit breaker or unusual trading volume without a stated business reason",
    "movers_list": "A list or roundup of stocks moving today, without company-specific news",
    "deal": "Merger, acquisition, SPAC or business combination, strategic review, asset sale",
    "legal_listing": "Lawsuit, investigation, delisting notice, compliance or listing issue, auditor issue",
    "business_update": "Contract, partnership, product, management change or other company announcement",
    "other": "None of the above",
}
MOVE_WORD = {True: "fell", False: "rose"}


def questions(drop: bool) -> dict:
    d = "decline" if drop else "rise"
    return {
        "category": {"type": "choice", "criteria": CATEGORIES,
                     "instructions": "What kind of news is this about the company with the stock ticker `ticker`?"},
        "next_week": {"type": "choice",
                      "instructions": f"In the hour after this news the stock `ticker` {MOVE_WORD[drop]} as described in "
                                      "`first_hour_move`. Over the next five trading days, what is most likely?",
                      "criteria": {"continues": f"The {d} continues: the stock moves further in the same direction",
                                   "holds": f"The stock stays around its new level",
                                   "reverses": f"The {d} partly or fully reverses"}},
        "explains": {"type": "noul",
                     "instructions": "Does the news give a clear company-specific reason for the move in `first_hour_move`?"},
        "lasting": {"type": "noul",
                    "instructions": "Does this news reveal a lasting change in the company's business or finances, rather "
                                    "than a one-off, technical or market-wide event?"},
        "dilution": {"type": "noul",
                     "instructions": "Does this news involve the company issuing new shares or securities convertible into "
                                     "shares?"},
    }


def score():
    import jev_score as js  # ask() with retries
    load_dotenv(ROOT / ".env")
    import study_drift as sd
    evs = [e for e in sd.EV if abs(e["excess_1h"]) >= 0.10]
    summaries = {}
    ids = {e["news_id"] for e in evs}
    for f in ("laya_alpaca_labels_2021_2023.jsonl", "laya_alpaca_labels_v2.jsonl"):
        for line in open(ROOT / "data" / f):
            if '"summary": ""' in line:
                continue
            r = json.loads(line)
            if r["news_id"] in ids and r.get("summary"):
                summaries[r["news_id"]] = r["summary"]
    done = {(r["news_id"], r["ticker"]) for r in map(json.loads, open(OUT))} if OUT.exists() else set()
    todo = [e for e in evs if (e["news_id"], e["ticker"]) not in done]
    print(f"{len(evs)} events with |first hour| >= 10%, {len(todo)} to score, {len(summaries)} with a summary", flush=True)
    lock, n, tok = threading.Lock(), 0, 0

    def one(e):
        nonlocal n, tok
        drop = e["excess_1h"] < 0
        state = {"ticker": e["ticker"], "headline": e["headline"], "summary": summaries.get(e["news_id"], "")[:2000],
                 "first_hour_move": f"{e['excess_1h'] * 100:+.0f}% relative to the overall market in the hour after the news"}
        body = {"model": js.MODEL, "state": state, "questions": questions(drop)}
        try:
            a = js.ask_body(body)
        except Exception as exc:
            print("error", e["ticker"], e["day"], exc, flush=True)
            return
        ans = a["answers"]
        row = {"news_id": e["news_id"], "ticker": e["ticker"], "day": e["day"], "drop": drop,
               "category": ans["category"]["choice"], "category_p": ans["category"]["probabilities"],
               "next_week": ans["next_week"]["probabilities"], "explains": ans["explains"]["noul"],
               "lasting": ans["lasting"]["noul"], "dilution": ans["dilution"]["noul"],
               "tokens": a["usage"]["input_tokens"], "model": a["model"]}
        with lock:
            with open(OUT, "a") as f:
                f.write(json.dumps(row) + "\n")
            n += 1
            tok += row["tokens"]
            if n % 500 == 0:
                print(f"{n}/{len(todo)} scored, ${tok / 1e6 * 0.042:.2f}", flush=True)

    with ThreadPoolExecutor(16) as ex:
        list(ex.map(one, todo))
    print(f"done: {n} scored, ${tok / 1e6 * 0.042:.2f}", flush=True)


def analyze():
    import study_drift as sd
    jev = {(r["news_id"], r["ticker"]): r for r in map(json.loads, open(OUT))}
    for drop in (True, False):
        side_evs = [e for e in sd.EV if (e["excess_1h"] <= -0.10 if drop else e["excess_1h"] >= 0.10)
                    and (e["news_id"], e["ticker"]) in jev]
        pairs = [(e, sd.short_trade(e)) for e in side_evs]
        pairs = [(e, x) for e, x in pairs if x is not None]
        j = lambda e: jev[(e["news_id"], e["ticker"])]
        print(f"\n######## {'DROPS <= -10%' if drop else 'POPS >= +10%'}: short at the close, 5 days ({len(pairs)} trades)")
        sd.stats(pairs, "all", True)
        print("-- by Jev's news category")
        for c in CATEGORIES:
            sd.stats([(e, x) for e, x in pairs if j(e)["category"] == c], c, True)
        print("-- by Jev's answers (terciles)")
        for name, f in (("P(continues)", lambda r: r["next_week"]["continues"]),
                        ("P(reverses)", lambda r: r["next_week"]["reverses"]),
                        ("explains", lambda r: r["explains"]), ("lasting", lambda r: r["lasting"]),
                        ("dilution", lambda r: r["dilution"])):
            v = sorted(f(j(e)) for e, _ in pairs)
            lo, hi = v[len(v) // 3], v[2 * len(v) // 3]
            sd.stats([(e, x) for e, x in pairs if f(j(e)) < lo], f"{name} < {lo:.2f}")
            sd.stats([(e, x) for e, x in pairs if lo <= f(j(e)) < hi], f"{name} {lo:.2f}..{hi:.2f}")
            sd.stats([(e, x) for e, x in pairs if f(j(e)) >= hi], f"{name} >= {hi:.2f}")
        # out-of-sample: choose the categories to skip on 2021-2024 (mean short P&L < 0), apply to 2025-2026
        train = [(e, x) for e, x in pairs if e["day"] < "2025-01-01"]
        test = [(e, x) for e, x in pairs if e["day"] >= "2025-01-01"]
        by = defaultdict(list)
        for e, x in train:
            by[j(e)["category"]].append(x)
        skip = {c for c, v in by.items() if len(v) >= 20 and sum(v) / len(v) < 0}
        print(f"-- out of sample: categories with a losing mean in 2021-24 (n>=20): {sorted(skip) or 'none'}")
        sd.stats(test, "2025-26 all")
        sd.stats([(e, x) for e, x in test if j(e)["category"] not in skip], "2025-26 without those categories")
        v = sorted(j(e)["next_week"]["reverses"] for e, _ in train)
        cut = v[2 * len(v) // 3]
        sd.stats([(e, x) for e, x in test if j(e)["next_week"]["reverses"] < cut],
                 f"2025-26 P(reverses) < {cut:.2f} (2021-24 top tercile cut)")


if __name__ == "__main__":
    {"score": score, "analyze": analyze}[sys.argv[1]]()
