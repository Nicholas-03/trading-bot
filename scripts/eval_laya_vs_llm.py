"""Compare base Laya, fine-tuned Laya and the LLM decisions on the held-out test news.

    python scripts/eval_laya_vs_llm.py [--preds models/laya-trading/test_predictions.jsonl] [--gate 0.70]

Unit: one (news, ticker) pair, labelled with the next-hour excess return vs SPY. An LLM made one decision per
news event, so its answer for a pair is its buy/short if it picked that ticker, else hold.
"Signed excess" = excess return for a buy, minus excess return for a short: what the call earned vs SPY.
"""
import argparse
import json
import sqlite3
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OPTIONS = ["buy", "short", "hold"]


def score(rows, gate):
    """rows: [(pred, confidence, label, excess)]"""
    out = {}
    for name, g in (("all", 0.0), ("gated", gate)):
        preds = [(p if p != "hold" and c >= g else "hold", lab, ex) for p, c, lab, ex in rows]
        f1s = []
        for k in OPTIONS:
            tp = sum(p == k and lab == k for p, lab, _ in preds)
            fp = sum(p == k and lab != k for p, lab, _ in preds)
            fn = sum(p != k and lab == k for p, lab, _ in preds)
            f1s.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
        signed = [ex if p == "buy" else -ex for p, _, ex in preds if p != "hold"]
        out[name] = {
            "acc": sum(p == lab for p, lab, _ in preds) / len(preds),
            "macro_f1": sum(f1s) / 3,
            "calls": len(signed),
            "hit": sum(s > 0 for s in signed) / len(signed) if signed else float("nan"),
            "mean_bp": 1e4 * sum(signed) / len(signed) if signed else float("nan"),
            "sum_pct": 100 * sum(signed),
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preds", default=str(ROOT / "models" / "laya-trading" / "test_predictions.jsonl"))
    ap.add_argument("--db", default=str(ROOT / "data" / "trades.db"))
    ap.add_argument("--gate", type=float, default=0.70)
    args = ap.parse_args()

    by_model = defaultdict(list)
    pairs = {}
    for line in open(args.preds):
        r = json.loads(line)
        pred = max(r["probs"], key=r["probs"].get)
        by_model[r["model"]].append((pred, r["confidence"], r["label"], r["excess_1h"]))
        pairs[(r["news_id"], r["ticker"])] = (r["label"], r["excess_1h"])

    con = sqlite3.connect(args.db)
    llm = defaultdict(dict)
    for nid, prov, action, ticker, conf in con.execute(
        "SELECT news_event_id, provider, action, ticker, confidence FROM llm_decisions"
    ):
        llm[prov][nid] = (action, (ticker or "").upper(), conf or 0.0)
    for prov, decisions in sorted(llm.items()):
        rows = []
        for (nid, ticker), (label, excess) in pairs.items():
            if nid not in decisions:
                continue
            action, t, conf = decisions[nid]
            pred = action if action in ("buy", "short") and t == ticker else "hold"
            rows.append((pred, conf, label, excess))
        if rows:
            by_model[prov] = rows

    labels = [lab for lab, _ in pairs.values()]
    print(f"test pairs: {len(pairs)}  labels: " + ", ".join(f"{k}={labels.count(k)}" for k in OPTIONS))
    print(f"majority-class (always hold) accuracy: {labels.count('hold') / len(labels):.3f}\n")
    head = f"{'model':<11}{'pairs':>6} | {'acc':>6}{'mF1':>6}{'calls':>6}{'hit':>6}{'bp/call':>8} | " \
           f"{'acc':>6}{'mF1':>6}{'calls':>6}{'hit':>6}{'bp/call':>8}{'sum %':>8}"
    print(f"{'':<17} | {'every non-hold call':^32} | {f'only confidence >= {args.gate}':^40}")
    print(head)
    print("-" * len(head))
    for name in ["laya_base", "laya_ft", "chatgpt", "claude", "gemini", "deepseek"]:
        if name not in by_model:
            continue
        s = score(by_model[name], args.gate)
        a, g = s["all"], s["gated"]
        print(f"{name:<11}{len(by_model[name]):>6} | {a['acc']:>6.3f}{a['macro_f1']:>6.3f}{a['calls']:>6}"
              f"{a['hit']:>6.2f}{a['mean_bp']:>8.1f} | {g['acc']:>6.3f}{g['macro_f1']:>6.3f}{g['calls']:>6}"
              f"{g['hit']:>6.2f}{g['mean_bp']:>8.1f}{g['sum_pct']:>8.2f}")


if __name__ == "__main__":
    main()
