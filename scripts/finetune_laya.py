"""Fine-tune Laya on our news, labelled with the next-hour price move (scripts/build_laya_price_labels.py).

    .venv/bin/python scripts/finetune_laya.py [--epochs 4] [--base convaiinnovations/laya] [--limit 64]

Each (news, ticker) pair becomes the exact question the bot asks live (advisor/laya_advisor.py), encoded with
Laya's own Agent._encode_state, so training and inference see identical token sequences. Loss is cross-entropy
over the three option logits (buy / short / hold). The token-embedding table is frozen.

Split by news time (no pair of one news event crosses splits): 70% train, 15% valid, 15% test.
The epoch is picked on valid macro-F1, then one temperature is fitted on valid so `confidence` is calibrated.
Output: models/laya-trading/ in Laya's checkpoint format (LAYA_MODEL_ID=models/laya-trading), plus
models/laya-trading/test_predictions.jsonl for scripts/eval_laya_vs_llm.py.
"""
import argparse
import json
import math
import os
import random
import shutil
import sys
import time
from pathlib import Path

os.environ.setdefault("USE_TF", "0")

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn.functional as F  # noqa: E402
from safetensors.torch import save_file  # noqa: E402

import laya  # noqa: E402
from laya.agent import Agent  # noqa: E402
from laya.common import collate_items, confidence_from_probs  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from advisor.laya_advisor import _build_questions, _build_state  # noqa: E402

OPTIONS = list(_build_questions("none")["action"]["criteria"])  # ["buy", "short", "hold"]
QUESTION = _build_questions("none")


def load_rows(path):
    rows = [json.loads(line) for line in open(path)]
    news_ids = sorted({r["news_id"] for r in rows}, key=lambda n: next(r["ts"] for r in rows if r["news_id"] == n))
    a, b = int(0.70 * len(news_ids)), int(0.85 * len(news_ids))
    split_of = {n: ("train" if i < a else "valid" if i < b else "test") for i, n in enumerate(news_ids)}
    out = {"train": [], "valid": [], "test": []}
    for r in rows:
        out[split_of[r["news_id"]]].append(r)
    return out


def encode(agent, rows):
    internal = {"action": Agent._to_internal(QUESTION["action"])}
    groups = []
    for r in rows:
        state = _build_state(r["headline"], r["summary"], r["ticker"], "none")
        item = agent._encode_state(state, ["action"], internal)[0]
        item["label"] = OPTIONS.index(r["label"])
        groups.append([item])
    return groups


def to_dev(b, dev):
    return [b[k].to(dev) for k in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")]


@torch.no_grad()
def logits_of(model, groups, pad_id, dev, bs=32):
    model.eval()
    out = []
    for i in range(0, len(groups), bs):
        b = collate_items(groups[i:i + bs], pad_id)
        logits, _ = model(*to_dev(b, dev))
        out.append(logits[:, :3].float().cpu().numpy())
    return np.concatenate(out)


def softmax(z, t=1.0):
    z = z / t
    e = np.exp(z - z.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


def metrics(probs, rows):
    y = np.array([OPTIONS.index(r["label"]) for r in rows])
    pred = probs.argmax(-1)
    f1s = []
    for k in range(3):
        tp, fp, fn = ((pred == k) & (y == k)).sum(), ((pred == k) & (y != k)).sum(), ((pred != k) & (y == k)).sum()
        f1s.append(2 * tp / (2 * tp + fp + fn) if tp else 0.0)
    nll = -np.mean(np.log(probs[np.arange(len(y)), y] + 1e-9))
    excess = np.array([r["excess_1h"] for r in rows])
    sign = np.where(pred == 0, 1.0, np.where(pred == 1, -1.0, 0.0))
    acted = sign != 0
    return {
        "acc": float((pred == y).mean()), "macro_f1": float(np.mean(f1s)), "nll": float(nll),
        "f1_buy": f1s[0], "f1_short": f1s[1], "f1_hold": f1s[2],
        "calls": int(acted.sum()),
        "mean_signed_excess_pct": float((sign * excess)[acted].mean() * 100) if acted.any() else 0.0,
    }


def fit_temperature(z, rows):
    y = np.array([OPTIONS.index(r["label"]) for r in rows])
    grid = np.exp(np.linspace(np.log(0.5), np.log(5.0), 60))
    nll = [-np.mean(np.log(softmax(z, t)[np.arange(len(y)), y] + 1e-9)) for t in grid]
    return float(grid[int(np.argmin(nll))])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--labels", default=str(ROOT / "data" / "laya_price_labels.jsonl"))
    ap.add_argument("--base", default="convaiinnovations/laya")
    ap.add_argument("--subfolder", default=None)
    ap.add_argument("--epochs", type=int, default=4)
    ap.add_argument("--lr", type=float, default=2e-5, help="encoder learning rate")
    ap.add_argument("--head-lr", type=float, default=1e-4, help="decision head + scorer learning rate")
    ap.add_argument("--batch", type=int, default=16)
    ap.add_argument("--limit", type=int, help="train on the first N pairs only (dry run)")
    ap.add_argument("--out", default=str(ROOT / "models" / "laya-trading"))
    ap.add_argument("--seed", type=int, default=42)
    args = ap.parse_args()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    dev = torch.device("mps" if torch.backends.mps.is_available() else "cpu")

    data = load_rows(args.labels)
    if args.limit:
        data["train"] = data["train"][: args.limit]
    for s, rows in data.items():
        counts = {o: sum(r["label"] == o for r in rows) for o in OPTIONS}
        print(f"{s:<6} {len(rows):5d} pairs  {counts}  {rows[0]['ts'][:16]} .. {rows[-1]['ts'][:16]}", flush=True)

    agent = laya.load(args.base, subfolder=args.subfolder, device=str(dev))
    model, pad_id = agent.model.float(), agent.tok.pad_token_id
    enc = {s: encode(agent, rows) for s, rows in data.items()}

    base_t = agent.temperature_by_options.get("choice:3-5", agent.temperature[0])
    base_valid = metrics(softmax(logits_of(model, enc["valid"], pad_id, dev), base_t), data["valid"])
    base_test_z = logits_of(model, enc["test"], pad_id, dev)
    print("base valid:", {k: round(v, 3) for k, v in base_valid.items()}, flush=True)

    model.encoder.embeddings.tok_embeddings.weight.requires_grad_(False)
    enc_p = [p for n, p in model.named_parameters() if p.requires_grad and n.startswith("encoder.")]
    head_p = [p for n, p in model.named_parameters() if p.requires_grad and not n.startswith("encoder.")]
    opt = torch.optim.AdamW([{"params": enc_p, "lr": args.lr}, {"params": head_p, "lr": args.head_lr}], weight_decay=0.01)
    steps = args.epochs * math.ceil(len(enc["train"]) / args.batch)
    warm = max(1, int(0.06 * steps))
    sched = torch.optim.lr_scheduler.LambdaLR(
        opt, lambda s: (s + 1) / warm if s < warm else max(0.0, (steps - s) / max(1, steps - warm)))

    best, history, step, t0 = None, [{"epoch": 0, **base_valid}], 0, time.time()
    for epoch in range(1, args.epochs + 1):
        order = list(range(len(enc["train"]))); random.shuffle(order)
        model.train(); run = 0.0
        for i in range(0, len(order), args.batch):
            b = collate_items([enc["train"][j] for j in order[i:i + args.batch]], pad_id)
            logits, _ = model(*to_dev(b, dev))
            loss = F.cross_entropy(logits[:, :3], b["label"].to(dev))
            opt.zero_grad(set_to_none=True); loss.backward()
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            opt.step(); sched.step(); step += 1
            run = 0.95 * run + 0.05 * loss.item() if step > 1 else loss.item()
            if step % 25 == 0:
                print(f"epoch {epoch} step {step}/{steps} loss {run:.4f} {time.time() - t0:.0f}s", flush=True)
        z = logits_of(model, enc["valid"], pad_id, dev)
        ev = metrics(softmax(z), data["valid"])
        history.append({"epoch": epoch, **ev})
        print(f"epoch {epoch} valid:", {k: round(v, 3) for k, v in ev.items()}, flush=True)
        if best is None or ev["macro_f1"] > best["macro_f1"]:
            temp = fit_temperature(z, data["valid"])
            best = {"epoch": epoch, "temperature": temp, **ev}
            save(model, agent, args, best, history, data)
            test_z = logits_of(model, enc["test"], pad_id, dev)
            print(f"saved epoch {epoch} (temperature {temp:.2f}) to {args.out}", flush=True)

    test_ft = metrics(softmax(test_z, best["temperature"]), data["test"])
    test_base = metrics(softmax(base_test_z, base_t), data["test"])
    print("\nTEST base      :", {k: round(v, 3) for k, v in test_base.items()})
    print("TEST fine-tuned:", {k: round(v, 3) for k, v in test_ft.items()})
    print(f"best epoch {best['epoch']}, total {time.time() - t0:.0f}s")

    with open(Path(args.out) / "test_predictions.jsonl", "w") as f:
        for r, zb, zf in zip(data["test"], base_test_z, test_z):
            for name, z, t in (("laya_base", zb, base_t), ("laya_ft", zf, best["temperature"])):
                p = softmax(z, t)
                f.write(json.dumps({"model": name, "news_id": r["news_id"], "ticker": r["ticker"],
                                    "label": r["label"], "excess_1h": r["excess_1h"],
                                    "probs": dict(zip(OPTIONS, map(float, p))),
                                    "confidence": confidence_from_probs(p, 3)}) + "\n")


def save(model, agent, args, best, history, data):
    out = Path(args.out); out.mkdir(parents=True, exist_ok=True)
    if Path(args.base).exists():
        src = Path(args.base)
    else:
        from huggingface_hub import snapshot_download
        src = Path(snapshot_download(args.base, local_files_only=True))
    if args.subfolder:
        src = src / args.subfolder
    for d in ("tokenizer", "encoder"):
        shutil.copytree(src / d, out / d, dirs_exist_ok=True)
    state = {k: v.detach().to("cpu", torch.float32).contiguous() for k, v in model.state_dict().items()}
    save_file(state, str(out / "model.safetensors"))
    cfg = dict(agent.cfg)
    cfg["temperature_by_options"] = {**cfg.get("temperature_by_options", {}), "choice:3-5": best["temperature"]}
    cfg["training"] = {**cfg.get("training", {}), "fine_tuned_from_checkpoint": True,
                       "trading_finetune": {"base": args.base, "subfolder": args.subfolder, "epoch": best["epoch"],
                                            "labels": "next-hour excess return vs SPY, +-1%",
                                            "pairs": {s: len(r) for s, r in data.items()},
                                            "lr": args.lr, "head_lr": args.head_lr, "seed": args.seed,
                                            "frozen": "encoder.embeddings.tok_embeddings", "history": history}}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()
