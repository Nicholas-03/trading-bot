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
from config import _DEFAULT_SHORT_LIQUID_SYMBOLS  # noqa: E402

OPTIONS = list(_build_questions("none")["action"]["criteria"])  # ["buy", "short", "hold"]
QUESTION = _build_questions("none")


def load_rows(path, max_tickers=None, tradable_only=False, label_field="label", threshold=1.0):
    rows = [json.loads(line) for line in open(path)]
    if max_tickers:
        rows = [r for r in rows if r["n_tickers"] <= max_tickers]
    if tradable_only:
        rows = [r for r in rows if r.get("tradable", True)]
    for r in rows:
        if label_field in ("label_ret", "label_excess"):  # 1h return (raw, or minus SPY's), no bracket
            x = (r["ret_1h"] if label_field == "label_ret" else r["excess_1h"]) * 100
            r["label"] = "buy" if x >= threshold else "short" if x <= -threshold else "hold"
        else:
            r["label"] = r[label_field]
    first_ts = {}
    for r in rows:
        first_ts[r["news_id"]] = min(first_ts.get(r["news_id"], r["ts"]), r["ts"])
    news_ids = sorted(first_ts, key=lambda n: (first_ts[n], n))
    a, b = int(0.70 * len(news_ids)), int(0.85 * len(news_ids))
    split_of = {n: ("train" if i < a else "valid" if i < b else "test") for i, n in enumerate(news_ids)}
    out = {"train": [], "valid": [], "test": []}
    for r in rows:
        out[split_of[r["news_id"]]].append(r)
    return out


def encode(agent, rows, with_reaction=False):
    internal = {"action": Agent._to_internal(QUESTION["action"])}
    groups = []
    for r in rows:
        state = _build_state(r["headline"], r["summary"], r["ticker"], "none",
                             r.get("react") if with_reaction else None)
        item = agent._encode_state(state, ["action"], internal)[0]
        item["label"] = OPTIONS.index(r["label"])
        groups.append([item])
    return groups


def to_dev(b, dev):
    return [b[k].to(dev) for k in ("input_ids", "attention_mask", "marker_pos", "marker_mask", "qtype")]


@torch.no_grad()
def logits_of(model, groups, pad_id, dev, bs=64):
    model.eval()
    order = sorted(range(len(groups)), key=lambda i: len(groups[i][0]["ids"]))  # less padding
    out = np.zeros((len(groups), 3), dtype=np.float32)
    with torch.autocast(dev.type, dtype=torch.float16, enabled=dev.type == "cuda"):
        for i in range(0, len(order), bs):
            idx = order[i:i + bs]
            b = collate_items([groups[j] for j in idx], pad_id)
            logits, _ = model(*to_dev(b, dev))
            out[idx] = logits[:, :3].float().cpu().numpy()
    return out


def softmax(z, t=1.0):
    z = z / t
    e = np.exp(z - z.max(-1, keepdims=True))
    return e / e.sum(-1, keepdims=True)


PNL = "raw"


def trade_pnl(rows):
    """% result of a long and of a short per row: the raw 1h return (no stop-loss / take-profit), or with
    --pnl bracket the simulated 2%/3% bracket trade."""
    field = "excess_1h" if PNL == "excess" else "ret_1h"  # excess: long/short the stock hedged with SPY
    ret = np.array([r.get(field, r["excess_1h"]) for r in rows]) * 100
    bracket = PNL == "bracket"
    long_ = np.array([r["sim_long"] * 100 if bracket and "sim_long" in r else x for r, x in zip(rows, ret)])
    short = np.array([r["sim_short"] * 100 if bracket and "sim_short" in r else -x for r, x in zip(rows, ret)])
    shortable = np.array([r["ticker"] in _DEFAULT_SHORT_LIQUID_SYMBOLS or PNL == "excess" for r in rows])
    return long_, short, shortable


def trade_stats(probs, rows, cost_pct):
    """Net % per trade (bracket result minus cost) and win rate at several confidence gates.
    Shorts outside the bot's liquid allowlist are skipped, as they are live."""
    pred = probs.argmax(-1)
    long_, short, shortable = trade_pnl(rows)
    conf = np.array([confidence_from_probs(p, 3) for p in probs])
    out = {}
    for gate in (0.0, 0.3, 0.5, 0.7):
        acted = ((pred == 0) | ((pred == 1) & shortable)) & (conf >= gate)
        pnl = np.where(pred == 0, long_, short)[acted] - cost_pct
        out[f"c{gate:.1f}"] = (int(acted.sum()), round(float(pnl.mean()), 3) if acted.any() else 0.0,
                               round(float((pnl > 0).mean()), 2) if acted.any() else 0.0)
    # Rank gates, independent of calibration: buy the top q by p(buy) - p(short), short the bottom q.
    score = probs[:, 0] - probs[:, 1]
    for q in (0.10, 0.02, 0.005):
        lo, hi = np.quantile(score, [q, 1 - q])
        acted = (score >= hi) | ((score <= lo) & shortable)
        pnl = np.where(score >= hi, long_, short)[acted] - cost_pct
        out[f"top{q:g}"] = (int(acted.sum()), round(float(pnl.mean()), 3) if acted.any() else 0.0,
                            round(float(pnl.std() / np.sqrt(max(1, acted.sum()))), 3) if acted.any() else 0.0)
    return out


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
    ap.add_argument("--class-weights", choices=["balanced", "none"], default="balanced",
                    help="balanced: weight each label by 1/frequency so the model can't win by always saying hold")
    ap.add_argument("--max-tickers", type=int, help="drop news that mention more than N tickers")
    ap.add_argument("--cost-pct", type=float, default=0.10, help="round-trip trading cost in percent")
    ap.add_argument("--label-field", default="label",
                    help="label (1h excess vs SPY), label_ret (raw 1h return) or label_sim (2%%/3%% bracket trade)")
    ap.add_argument("--label-threshold", type=float, default=1.0, help="percent move for buy/short with label_ret")
    ap.add_argument("--pnl", choices=["raw", "bracket", "excess"], default="raw",
                    help="how trades are scored (excess: the 1h move minus SPY's, i.e. hedged)")
    ap.add_argument("--tradable-only", action="store_true", help="drop pairs the bot's price/liquidity gates would skip")
    ap.add_argument("--hold-frac", type=float, default=1.0, help="keep this fraction of train 'hold' pairs")
    ap.add_argument("--max-train", type=int, help="subsample train to at most N pairs (after --hold-frac)")
    ap.add_argument("--with-reaction", action="store_true",
                    help="give the model the price move from the news to entry (needs react in the labels)")
    ap.add_argument("--max-eval", type=int, help="subsample valid and test to at most N pairs each")
    ap.add_argument("--freeze-layers", type=int, default=0, help="freeze the embeddings and the first N encoder layers")
    ap.add_argument("--grad-ckpt", action="store_true", help="gradient checkpointing (less GPU memory, slower)")
    args = ap.parse_args()
    random.seed(args.seed); np.random.seed(args.seed); torch.manual_seed(args.seed)
    dev = torch.device("cuda" if torch.cuda.is_available() else "mps" if torch.backends.mps.is_available() else "cpu")

    global PNL
    PNL = args.pnl
    data = load_rows(args.labels, args.max_tickers, args.tradable_only, args.label_field, args.label_threshold)
    rng = random.Random(args.seed)
    if args.hold_frac < 1:
        data["train"] = [r for r in data["train"] if r["label"] != "hold" or rng.random() < args.hold_frac]
    if args.max_train and len(data["train"]) > args.max_train:
        data["train"] = sorted(rng.sample(data["train"], args.max_train), key=lambda r: r["ts"])
    for split in ("valid", "test"):
        if args.max_eval and len(data[split]) > args.max_eval:
            data[split] = sorted(rng.sample(data[split], args.max_eval), key=lambda r: r["ts"])
    if args.limit:
        data["train"] = data["train"][: args.limit]
        data["valid"] = data["valid"][: args.limit]
        data["test"] = data["test"][: args.limit]
    for s, rows in data.items():
        counts = {o: sum(r["label"] == o for r in rows) for o in OPTIONS}
        print(f"{s:<6} {len(rows):5d} pairs  {counts}  {rows[0]['ts'][:16]} .. {rows[-1]['ts'][:16]}", flush=True)

    agent = laya.load(args.base, subfolder=args.subfolder, device=str(dev))
    model, pad_id = agent.model.float(), agent.tok.pad_token_id
    enc = {s: encode(agent, rows, args.with_reaction) for s, rows in data.items()}

    base_t = agent.temperature_by_options.get("choice:3-5", agent.temperature[0])
    base_valid = metrics(softmax(logits_of(model, enc["valid"], pad_id, dev), base_t), data["valid"])
    base_test_z = logits_of(model, enc["test"], pad_id, dev)
    print("base valid:", {k: round(v, 3) for k, v in base_valid.items()}, flush=True)

    counts = np.bincount([OPTIONS.index(r["label"]) for r in data["train"]], minlength=3)
    weight = (counts.sum() / (3 * np.maximum(counts, 1)) if args.class_weights == "balanced" else np.ones(3))
    weight = torch.tensor(weight, dtype=torch.float32, device=dev)
    print("class weights:", dict(zip(OPTIONS, weight.tolist())), flush=True)

    model.encoder.embeddings.tok_embeddings.weight.requires_grad_(False)
    if args.freeze_layers:
        model.encoder.embeddings.requires_grad_(False)
        for layer in model.encoder.layers[: args.freeze_layers]:
            layer.requires_grad_(False)
    if args.grad_ckpt:
        model.encoder.gradient_checkpointing_enable(gradient_checkpointing_kwargs={"use_reentrant": False})
        model.head_checkpointing = True
    amp = dev.type == "cuda"
    scaler = torch.amp.GradScaler("cuda", enabled=amp)
    print(f"trainable params {sum(p.numel() for p in model.parameters() if p.requires_grad) / 1e6:.0f}M, amp={amp}",
          flush=True)
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
            with torch.autocast(dev.type, dtype=torch.float16, enabled=amp):
                logits, _ = model(*to_dev(b, dev))
            loss = F.cross_entropy(logits[:, :3].float(), b["label"].to(dev), weight=weight)
            opt.zero_grad(set_to_none=True); scaler.scale(loss).backward(); scaler.unscale_(opt)
            torch.nn.utils.clip_grad_norm_([p for p in model.parameters() if p.requires_grad], 1.0)
            scaler.step(opt); scaler.update(); sched.step(); step += 1
            run = 0.95 * run + 0.05 * loss.item() if step > 1 else loss.item()
            if step % 200 == 0:
                print(f"epoch {epoch} step {step}/{steps} loss {run:.4f} {time.time() - t0:.0f}s", flush=True)
        z = logits_of(model, enc["valid"], pad_id, dev)
        ev = metrics(softmax(z), data["valid"])
        history.append({"epoch": epoch, **ev})
        print(f"epoch {epoch} valid:", {k: round(v, 3) for k, v in ev.items()}, flush=True)
        print(f"epoch {epoch} valid trades (n, net %/trade, win rate):",
              trade_stats(softmax(z, fit_temperature(z, data["valid"])), data["valid"], args.cost_pct), flush=True)
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
    print("TEST trades (n, net %/trade, win rate) base      :", trade_stats(softmax(base_test_z, base_t), data["test"], args.cost_pct))
    print("TEST trades (n, net %/trade, win rate) fine-tuned:", trade_stats(softmax(test_z, best["temperature"]), data["test"], args.cost_pct))
    long_, short, shortable = trade_pnl(data["test"])
    for name, r in (("always buy", long_), ("always short (allowlist)", short[shortable])):
        r = r - args.cost_pct
        print(f"TEST baseline {name}: n={len(r)} net %/trade={r.mean():.3f} win={(r > 0).mean():.2f}")
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
        src = Path(args.base) / (args.subfolder or "")
    else:
        from huggingface_hub import hf_hub_download  # the snapshot laya.load() cached (only the files it needs)
        src = Path(hf_hub_download(args.base, "rl_agent_config.json", subfolder=args.subfolder,
                                   local_files_only=True)).parent
    for d in ("tokenizer", "encoder"):
        shutil.copytree(src / d, out / d, dirs_exist_ok=True)
    state = {k: v.detach().to("cpu", torch.float32).contiguous() for k, v in model.state_dict().items()}
    save_file(state, str(out / "model.safetensors"))
    cfg = dict(agent.cfg)
    cfg["temperature_by_options"] = {**cfg.get("temperature_by_options", {}), "choice:3-5": best["temperature"]}
    cfg["training"] = {**cfg.get("training", {}), "fine_tuned_from_checkpoint": True,
                       "trading_finetune": {"base": args.base, "subfolder": args.subfolder, "epoch": best["epoch"],
                                            "labels": os.path.basename(args.labels),
                                            "class_weights": args.class_weights, "max_tickers": args.max_tickers,
                                            "label_field": args.label_field, "tradable_only": args.tradable_only,
                                            "hold_frac": args.hold_frac, "freeze_layers": args.freeze_layers,
                                            "with_reaction": args.with_reaction,
                                            "pairs": {s: len(r) for s, r in data.items()},
                                            "lr": args.lr, "head_lr": args.head_lr, "seed": args.seed,
                                            "frozen": "encoder.embeddings.tok_embeddings", "history": history}}
    (out / "rl_agent_config.json").write_text(json.dumps(cfg, indent=2))


if __name__ == "__main__":
    main()
