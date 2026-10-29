"""Score news/stock pairs with TypeSafe's Jev (zero-shot, typed answers) to compare with the fine-tuned Laya model.

    .venv/bin/python scripts/jev_score.py --pairs kaggle/laya-trading/results/xs_big/test_predictions.jsonl [--workers 16]

Pairs come from any JSONL with news_id and ticker (e.g. a Laya run's test predictions); the text comes from
data/laya_daily_labels.jsonl. Jev cannot be fine-tuned, so the questions carry the task: which way the stock moves
against the market over the next hour and the next week, how the news affects the company's value, whether it is new
information, and whether the company is the story's subject. No date goes into the state, to limit what Jev might
remember about the outcome. Answers are appended to data/jev_scores.jsonl (resumable: scored pairs are skipped).
Needs TYPESAFE_API_KEY in .env. Price: $0.042 per million input tokens (~600 tokens per pair).
"""
import argparse
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "data" / "jev_scores.jsonl"
URL = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"  # pinned, so answers stay comparable across runs

MOVE = {"up": "Rises clearly more than the overall market", "down": "Falls clearly more than the overall market",
        "flat": "Moves about in line with the overall market"}
QUESTIONS = {
    "move_1h": {"type": "choice", "criteria": MOVE,
                "instructions": "This news was just published. Over the next hour, how will the stock `ticker` most likely "
                                "move relative to the overall stock market?"},
    "move_5d": {"type": "choice", "criteria": MOVE,
                "instructions": "This news was just published. Over the next five trading days, how will the stock `ticker` "
                                "most likely move relative to the overall stock market?"},
    "impact": {"type": "score",
               "instructions": "How does this news affect the value of the company with the stock ticker `ticker`?",
               "criteria": ["Very negative for the company", "Somewhat negative for the company", "Neutral or unclear",
                            "Somewhat positive for the company", "Very positive for the company"]},
    "surprise": {"type": "noul",
                 "instructions": "Does this news give new, unexpected information about the company with the stock ticker "
                                 "`ticker`, rather than routine or already known information?"},
    "subject": {"type": "noul",
                "instructions": "Is the company with the stock ticker `ticker` the main subject of this news, rather than "
                                "one of several companies mentioned or a side mention?"},
}


def ask(state: dict) -> dict:
    return ask_body({"model": MODEL, "state": state, "questions": QUESTIONS})


def ask_body(request: dict, attempts: int = 8) -> dict:
    """POST one System One request, retrying rate limits, overloads and network errors with backoff."""
    body = json.dumps(request).encode()
    for attempt in range(attempts):
        req = urllib.request.Request(URL, data=body, headers={"Authorization": "Bearer " + os.environ["TYPESAFE_API_KEY"],
                                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 502, 503, 520, 522, 524, 529):
                raise RuntimeError(f"HTTP {e.code}: {e.read()[:300]!r}")
            time.sleep(float(e.headers.get("retry-after") or 0) or 2 ** attempt * (1 + random.random()))
        except (urllib.error.URLError, TimeoutError):
            time.sleep(2 ** attempt)
    raise RuntimeError("gave up after retries")


def flatten(a: dict) -> dict:
    """The answers as plain numbers: P(up)/P(down) per horizon, impact score (0-4), and the two yes/no probabilities."""
    ans = a["answers"]
    return {"p_up_1h": ans["move_1h"]["probabilities"]["up"], "p_down_1h": ans["move_1h"]["probabilities"]["down"],
            "p_up_5d": ans["move_5d"]["probabilities"]["up"], "p_down_5d": ans["move_5d"]["probabilities"]["down"],
            "impact": ans["impact"]["score"], "impact_conf": ans["impact"]["confidence"],
            "surprise": ans["surprise"]["noul"], "subject": ans["subject"]["noul"],
            "tokens": a["usage"]["input_tokens"], "model": a["model"]}


def main():
    load_dotenv(ROOT / ".env")
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", nargs="+", required=True)
    ap.add_argument("--workers", type=int, default=16)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    want = {}
    for f in args.pairs:
        for line in open(f):
            r = json.loads(line)
            want[(r["news_id"], r["ticker"])] = None
    done = {(r["news_id"], r["ticker"]) for r in map(json.loads, open(OUT))} if OUT.exists() else set()
    todo = [k for k in want if k not in done]
    text = {}
    ids = {k[0] for k in todo}
    for line in open(ROOT / "data" / "laya_daily_labels.jsonl"):
        r = json.loads(line)
        if r["news_id"] in ids:
            text[(r["news_id"], r["ticker"])] = r
    todo = [k for k in todo if k in text]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{len(want)} pairs, {len(done)} already scored, {len(todo)} to score", flush=True)
    lock, n, tok, t0 = threading.Lock(), 0, 0, time.time()

    def one(k):
        nonlocal n, tok
        r = text[k]
        state = {"ticker": r["ticker"], "headline": r["headline"], "summary": (r.get("summary") or "")[:2000],
                 "number_of_tickers_tagged_on_the_story": r["n_tickers"]}
        try:
            row = {"news_id": k[0], "ticker": k[1], **flatten(ask(state))}
        except Exception as exc:  # one bad pair must not stop the run
            print("error", k, exc, flush=True)
            return
        with lock:
            with open(OUT, "a") as f:
                f.write(json.dumps(row) + "\n")
            n += 1
            tok += row["tokens"]
            if n % 1000 == 0:
                print(f"{n}/{len(todo)} scored, {tok / 1e6:.1f}M tokens (${tok / 1e6 * 0.042:.2f}), "
                      f"{n / (time.time() - t0):.1f}/s", flush=True)

    with ThreadPoolExecutor(args.workers) as ex:
        list(ex.map(one, todo))
    print(f"done: {n} scored, {tok / 1e6:.1f}M tokens (${tok / 1e6 * 0.042:.2f})", flush=True)


if __name__ == "__main__":
    main()
