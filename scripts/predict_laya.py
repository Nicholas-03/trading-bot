"""Run a (fine-tuned) Laya checkpoint over a labels file and write its probabilities, for out-of-sample checks.

    python scripts/predict_laya.py --model models/laya-trading --labels data/laya_alpaca_labels_v2.jsonl \
        --start 2025-03-05 --out predictions.jsonl [--tradable-only] [--max-rows 60000] [--with-reaction]

Uses the same state encoding as the bot (advisor/laya_advisor.py) and the model's own calibrated temperature.
"""
import argparse
import json
import os
import random
import sys
from pathlib import Path

os.environ.setdefault("USE_TF", "0")

import numpy as np  # noqa: E402
import torch  # noqa: E402

import laya  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "scripts"))
from finetune_laya import OPTIONS, encode, logits_of, softmax  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True)
    ap.add_argument("--subfolder", default=None)
    ap.add_argument("--labels", required=True)
    ap.add_argument("--start", default="0000")
    ap.add_argument("--end", default="9999")
    ap.add_argument("--tradable-only", action="store_true")
    ap.add_argument("--with-reaction", action="store_true")
    ap.add_argument("--max-rows", type=int)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()

    rows = [json.loads(line) for line in open(args.labels)]
    rows = [r for r in rows if args.start <= r["ts"][:10] <= args.end and (r["tradable"] or not args.tradable_only)]
    if args.max_rows and len(rows) > args.max_rows:
        rows = sorted(random.Random(args.seed).sample(rows, args.max_rows), key=lambda r: r["ts"])
    for r in rows:
        r.setdefault("label", "hold")
    print(f"{len(rows)} pairs {rows[0]['ts'][:10]} .. {rows[-1]['ts'][:10]}", flush=True)

    dev = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")
    agent = laya.load(args.model, subfolder=args.subfolder, device=str(dev))
    model, pad_id = agent.model.float(), agent.tok.pad_token_id
    temp = agent.temperature_by_options.get("choice:3-5", agent.temperature[0])
    z = logits_of(model, encode(agent, rows, args.with_reaction), pad_id, dev)
    probs = softmax(z, temp)
    with open(args.out, "w") as f:
        for r, p in zip(rows, probs):
            f.write(json.dumps({"news_id": r["news_id"], "ticker": r["ticker"], "ts": r["ts"],
                                "probs": dict(zip(OPTIONS, map(float, p)))}) + "\n")
    print(f"wrote {len(rows)} predictions (temperature {temp:.2f}) to {args.out}", flush=True)


if __name__ == "__main__":
    main()
