"""Trade the rules, not the headline: on thin Polymarket markets, does reading the exact resolution rules beat the price?

    .venv/bin/python scripts/polymarket_rules.py collect [--per-quarter 400]   # closed thin markets + rules + prices
    .venv/bin/python scripts/polymarket_rules.py score [jev-1.13.0|clef-flash]  # ask the model, title-only and with rules
    .venv/bin/python scripts/polymarket_rules.py analyze

Idea: traders price the title's intuitive meaning; the rules (the market's `description`) sometimes demand more (an
official source, an exact threshold, a narrow window) or less. A System One model reads both and gives:
  - `gap`: are the rules stricter for YES than the title suggests, looser, or the same (a Choice);
  - P(YES) from the title alone and P(YES) with the rules (two Nouls, separate requests so they don't see each other).
Trade (fixed before looking at results): buy NO when the rules are stricter (P(stricter) >= 0.5), buy YES when looser,
hold to resolution; also the rules-minus-title shift in P(YES) as a signal. Return per $ after a 3-cent entry cost
(thin books; no exit cost since held to resolution). Compared with the same trade on all markets at the same prices,
because NO already wins on long shots (favourite-longshot bias, results/polymarket_calibration.txt).
Models: jev-1.13.0 (TypeSafe API, TYPESAFE_API_KEY) or a local System One model through Ollama (clef-flash).
"""
import json
import os
import re
import sys
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import polymarket_calibration as pc  # noqa: E402

DATA = ROOT / "data" / "polymarket_rules.jsonl"
COST = 0.03
# sports games and short crypto up/down markets: boilerplate rules, mostly listed < 7 days before the end
SKIP = re.compile(r"\bvs\.?\b|In the upcoming|scheduled to play|Up or Down|O/U|Spread:|win on 20\d\d-", re.I)
DAYS_BEFORE = 7
OLLAMA = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/") + "/v1/systemone"

GAP = {"type": "choice",
       "instructions": "A prediction market's title is in `title`; its official resolution rules are in `rules`. Traders "
                       "often read only the title. Compared with what a reasonable reader of the title alone would "
                       "expect, do the rules make a YES resolution harder, easier, or neither?",
       "criteria": {
           "stricter": "Harder: the rules add a requirement the title does not show, such as a specific official source "
                       "or confirmation, an exact threshold or measure, a narrower time window or time zone, or an "
                       "exclusion, so some outcomes a title reader would count as YES resolve NO.",
           "looser": "Easier: the rules count more outcomes as YES than the title suggests, such as an announcement or "
                     "report instead of the event itself, a broader definition, partial events, or a longer window.",
           "same": "Neither: the rules spell out what the title already means, with no difference that would change "
                   "how a typical outcome resolves."}}
YES_TITLE = {"type": "noul", "instructions": "Will the prediction-market question in `title` resolve YES by `end_date`? "
                                             "Judge as of `today`."}
YES_RULES = {"type": "noul", "instructions": "Will this prediction market resolve YES under its exact rules in `rules` "
                                             "(not the title's loose meaning) by `end_date`? Judge as of `today`."}


def day(t: float) -> str:
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%d")


def collect(per_quarter: int = 400) -> None:
    """Closed, cleanly resolved Yes/No markets with $1k-100k volume, priced DAYS_BEFORE days before the scheduled end."""
    quarters = [f"{y}-{m:02d}-01" for y in range(2024, 2027) for m in (1, 4, 7, 10)]
    quarters = [q for q in quarters if q <= "2026-10-01"]
    done = {json.loads(line)["token"] for line in open(DATA)} if DATA.exists() else set()
    cands = []
    for lo, hi in zip(quarters, quarters[1:]):
        offset, got = 0, 0
        while got < per_quarter * 3 and offset < 4900:
            page = pc.get(f"{pc.GAMMA}/markets", {"closed": "true", "limit": 100, "offset": offset, "order": "volumeNum",
                                                  "ascending": "false", "end_date_min": lo, "end_date_max": hi,
                                                  "volume_num_min": 1000, "volume_num_max": 100000})
            if not page or not isinstance(page, list):
                break
            offset += len(page)
            for m in page:
                try:
                    prices = [float(p) for p in json.loads(m["outcomePrices"])]
                    outcomes = json.loads(m["outcomes"])
                except (KeyError, TypeError, ValueError):
                    continue
                desc = (m.get("description") or "").strip()
                if outcomes != ["Yes", "No"] or sorted(prices) != [0.0, 1.0] or len(desc) < 80 \
                        or SKIP.search(m["question"]) or SKIP.search(desc[:200]):
                    continue
                tok = json.loads(m["clobTokenIds"])[0]
                if tok in done:
                    continue
                cands.append({"q": m["question"], "rules": desc[:6000], "source": m.get("resolutionSource") or "",
                              "yes": prices[0] == 1.0, "token": tok, "end": pc.ts(m.get("endDate")),
                              "closed": pc.ts(m.get("closedTime")) or pc.ts(m.get("endDate")),
                              "volume": float(m.get("volumeNum") or 0), "slug": m.get("slug")})
                got += 1
        print(f"{lo}: {got} candidates", flush=True)
    lock, n = threading.Lock(), 0

    def one(m):
        nonlocal n
        if not m["end"] or not m["closed"]:
            return
        t = m["end"] - DAYS_BEFORE * 86400
        if m["closed"] <= t:
            return
        d = pc.get(f"{pc.CLOB}/prices-history", {"market": m["token"], "startTs": int(t - 24 * 3600), "endTs": int(t),
                                                 "fidelity": 60})
        h = (d or {}).get("history") or []
        if not h or not 0.02 <= h[-1]["p"] <= 0.98:
            return
        with lock:
            with open(DATA, "a") as f:
                f.write(json.dumps({**m, "p": h[-1]["p"], "t": t}) + "\n")
            n += 1

    with ThreadPoolExecutor(8) as ex:
        list(ex.map(one, cands))
    print(f"{n} markets priced {DAYS_BEFORE} days before the end, 2-98c -> {DATA.name}", flush=True)


def ask(body: dict, model: str) -> dict:
    import urllib.error
    import urllib.request
    local = not model.startswith("jev")
    for attempt in range(8):
        req = urllib.request.Request(OLLAMA if local else "https://api.typesafe.ai/v1/systemone",
                                     data=json.dumps({**body, "model": model}).encode(),
                                     headers={"Content-Type": "application/json", **({} if local else {
                                         "Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"]})})
        try:
            with urllib.request.urlopen(req, timeout=300) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 520, 522, 524, 529):
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}")
            time.sleep(float(e.headers.get("retry-after") or 0) or 2 ** attempt)
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError("gave up")


def score(model: str = "jev-1.13.0") -> None:
    out = ROOT / "data" / f"polymarket_rules_{model.split('-')[0]}.jsonl"
    rows = [json.loads(line) for line in open(DATA)]
    done = {json.loads(line)["token"] for line in open(out)} if out.exists() else set()
    todo = [r for r in rows if r["token"] not in done]
    print(f"{len(rows)} markets, {len(todo)} to score with {model}", flush=True)
    lock, n = threading.Lock(), 0

    def one(r):
        nonlocal n
        base = {"title": r["q"], "today": day(r["t"]), "end_date": day(r["end"])}
        rules = {**base, "rules": r["rules"] + (f"\nResolution source: {r['source']}" if r["source"] else "")}
        try:
            a = ask({"state": rules, "questions": {"gap": GAP, "yes_rules": YES_RULES}}, model)
            b = ask({"state": base, "questions": {"yes_title": YES_TITLE}}, model)
        except Exception as exc:  # noqa: BLE001
            print("error", r["q"][:60], exc, flush=True)
            return
        g = a["answers"]["gap"]["probabilities"]
        row = {"token": r["token"], "stricter": g["stricter"], "looser": g["looser"],
               "yes_rules": a["answers"]["yes_rules"]["noul"], "yes_title": b["answers"]["yes_title"]["noul"]}
        with lock:
            with open(out, "a") as f:
                f.write(json.dumps(row) + "\n")
            n += 1
            if n % 200 == 0:
                print(f"  {n}/{len(todo)}", flush=True)

    with ThreadPoolExecutor(16 if model.startswith("jev") else 2) as ex:
        list(ex.map(one, todo))
    print(f"done: {n}", flush=True)


def ret(r: dict, side: str, cost: float = COST) -> float:
    """Per $ staked, buying `side` at the price plus `cost`, held to resolution."""
    if side == "yes":
        return r["yes"] / min(r["p"] + cost, 0.999) - 1
    return (1 - r["yes"]) / min(1 - r["p"] + cost, 0.999) - 1


def line(name: str, xs: list[float]) -> str:
    if len(xs) < 5:
        return f"  {name:58s} n={len(xs)}"
    a = np.array(xs)
    return f"  {name:58s} n={len(a):5d}  {a.mean():+7.1%} ± {a.std() / np.sqrt(len(a)):5.1%}  win {np.mean(a > 0):.0%}"


def analyze() -> None:
    base = {json.loads(x)["token"]: json.loads(x) for x in open(DATA)}
    print(f"{len(base)} thin markets (volume $1k-100k), YES price {DAYS_BEFORE} days before the end, 2-98c; "
          f"returns per $ to resolution after a {COST * 100:.0f}c entry cost\n")
    for model in ("jev", "clef"):
        f = ROOT / "data" / f"polymarket_rules_{model}.jsonl"
        if not f.exists():
            continue
        rs = [{**base[s["token"]], **s} for s in map(json.loads, open(f)) if s["token"] in base]
        print(f"== {model}: {len(rs)} scored")
        y = np.array([r["yes"] for r in rs], float)
        for k in ("p", "yes_title", "yes_rules"):
            print(f"  Brier {k:10s} {np.mean((np.array([r[k] for r in rs]) - y) ** 2):.4f}")
        strict = [r for r in rs if r["stricter"] >= 0.5]
        loose = [r for r in rs if r["looser"] >= 0.5]
        print(f"  gap: stricter {len(strict)}, looser {len(loose)}, YES rate stricter {np.mean([r['yes'] for r in strict] or [0]):.1%} "
              f"(avg price {np.mean([r['p'] for r in strict] or [0]):.1%}), looser {np.mean([r['yes'] for r in loose] or [0]):.1%} "
              f"(avg price {np.mean([r['p'] for r in loose] or [0]):.1%})")
        print(line("buy NO on all markets (baseline)", [ret(r, "no") for r in rs]))
        print(line("buy NO when rules stricter", [ret(r, "no") for r in strict]))
        print(line("buy YES on all markets (baseline)", [ret(r, "yes") for r in rs]))
        print(line("buy YES when rules looser", [ret(r, "yes") for r in loose]))
        for lo, hi in ((0.02, 0.2), (0.2, 0.5), (0.5, 0.8), (0.8, 0.98)):
            b = [r for r in rs if lo <= r["p"] < hi]
            print(line(f"  price {lo:.2f}-{hi:.2f}: buy NO all / stricter", [ret(r, "no") for r in b]) + " |" +
                  line("", [ret(r, "no") for r in b if r["stricter"] >= 0.5]).replace(" " * 60, ""))
        # rules shift: the model moves P(YES) when it reads the rules; trade the direction of the shift when it is big
        for th in (0.10, 0.20):
            tr = [ret(r, "yes" if r["yes_rules"] > r["yes_title"] else "no") for r in rs
                  if abs(r["yes_rules"] - r["yes_title"]) >= th]
            print(line(f"trade the rules shift |P_rules - P_title| >= {th:.2f}", tr))
            tr = [ret(r, "yes" if r["yes_rules"] > r["p"] else "no") for r in rs if abs(r["yes_rules"] - r["p"]) >= th
                  and (r["yes_rules"] - r["p"]) * (r["yes_rules"] - r["yes_title"]) > 0]
            print(line(f"rules shift agrees with |P_rules - price| >= {th:.2f}", tr))
        per = defaultdict(list)
        for r in strict:
            per[day(r["end"])[:4]].append(ret(r, "no"))
        print("  buy NO when stricter, by year: " + ", ".join(f"{k} {np.mean(v):+.1%} n={len(v)}" for k, v in sorted(per.items())))
        print()


if __name__ == "__main__":
    load_dotenv(ROOT / ".env")
    cmd, *rest = sys.argv[1:]
    {"collect": lambda *a: collect(*map(int, a)), "score": score, "analyze": analyze}[cmd](*rest)
