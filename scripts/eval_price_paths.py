"""Does the price/volume path around the news predict the direction of the next minutes? (gradient boosting, chronological split)

    uv run --with scikit-learn --with numpy scripts/eval_price_paths.py [data/laya_price_paths.jsonl]

Features are only what is known at entry (pre-news drift of the stock and SPY, the move / high / low / volume spike between the
news and entry, time of day, price level). Targets are the raw returns from entry at +5/15/30/60 minutes. Trades: long the top
quantile of the prediction, short the bottom quantile (shortable allowlist only), 0.10% round-trip cost, net % per trade ± SE.
"""
import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

import numpy as np
from sklearn.ensemble import HistGradientBoostingRegressor

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from config import _DEFAULT_SHORT_LIQUID_SYMBOLS as ALLOW  # noqa: E402

PRE = ("60", "30", "10", "5", "2", "1")
HORIZONS = ("5", "15", "30", "60")
COST = 0.10
TRAIN_END, VALID_END = "2025-11-18", "2026-04-21"
NAMES = ([f"pre{k}" for k in PRE] + [f"spy_pre{k}" for k in PRE]
         + ["move", "spy_move", "high", "low", "log_vol_ratio", "log_pre_vol", "minute_et", "log_price", "n_tickers", "allow"])


def load(path):
    rows = [json.loads(line) for line in open(path)]
    n_tickers = Counter(r["news_id"] for r in rows)
    X, Y, meta = [], defaultdict(list), []
    for r in rows:
        e = r["early"]
        f = lambda v: np.nan if v is None else v  # noqa: E731
        X.append([f(r["pre"][k]) for k in PRE] + [f(r["spy_pre"][k]) for k in PRE]
                 + [e["move"], e["spy_move"], f(e["high"]), f(e["low"]),
                    np.log(e["vol_ratio"] + 0.01) if e["vol_ratio"] is not None else np.nan,
                    np.log(e["pre_vol_min"] + 1), r["minute_et"], np.log(r["entry"]),
                    n_tickers[r["news_id"]], float(r["ticker"] in ALLOW)])
        for h in HORIZONS:
            Y[h].append(r["fwd"][h][0] * 100 if r["fwd"].get(h) else np.nan)
        meta.append((r["ts"][:10], r["ticker"] in ALLOW))
    return np.array(X, float), {h: np.array(v) for h, v in Y.items()}, meta


def trades(pred, y, allow, q):
    hi, lo = pred >= np.quantile(pred, 1 - q), (pred <= np.quantile(pred, q)) & allow
    out = {}
    for tag, sel, s in (("long", hi, 1), ("short", lo, -1)):
        x = s * y[sel] - COST
        out[tag] = (int(sel.sum()), x.mean() if len(x) else 0.0, x.std() / np.sqrt(max(1, len(x))))
    return out


def fmt(t):
    return "  ".join(f"{k} n={n:5d} {m:+.3f}±{se:.3f}" for k, (n, m, se) in t.items())


def main():
    X, Y, meta = load(sys.argv[1] if len(sys.argv) > 1 else ROOT / "data" / "laya_price_paths.jsonl")
    days = np.array([d for d, _ in meta])
    allow = np.array([a for _, a in meta])
    split = {"train": days < TRAIN_END, "valid": (days >= TRAIN_END) & (days < VALID_END), "test": days >= VALID_END}
    print({k: int(v.sum()) for k, v in split.items()})
    for h in HORIZONS:
        y = Y[h]
        ok = ~np.isnan(y)
        lo, hi = np.nanpercentile(y[split["train"] & ok], [1, 99])
        tr = split["train"] & ok
        m = HistGradientBoostingRegressor(max_iter=300, learning_rate=0.05, min_samples_leaf=200, random_state=0)
        m.fit(X[tr], np.clip(y[tr], lo, hi))
        print(f"\n=== +{h} min  (always long test: {np.nanmean(y[split['test']]) - COST:+.3f}%)")
        for name in ("valid", "test"):
            s = split[name] & ok
            p = m.predict(X[s])
            print(f" {name}: corr={np.corrcoef(p, y[s])[0, 1]:+.4f}")
            for q in (0.05, 0.01):
                print(f"   {q:.0%}: {fmt(trades(p, y[s], allow[s], q))}")
        # simple momentum check: follow the move between news and entry
        s = split["test"] & ok
        mv = X[s][:, NAMES.index("move")] * 100
        for thr in (0.5, 1.0, 2.0):
            up, dn = mv >= thr, (mv <= -thr) & allow[s]
            print(f"   follow move >={thr}%: long n={up.sum()} {np.mean(y[s][up]) - COST:+.3f}  "
                  f"short n={dn.sum()} {np.mean(-y[s][dn]) - COST if dn.any() else 0:+.3f}  "
                  f"fade: long-after-drop {np.mean(y[s][mv <= -thr]) - COST:+.3f}")
        if h == "60":
            s = split["test"] & ok
            p = m.predict(X[s])
            top = p >= np.quantile(p, 0.95)
            print("   test top-5% long by month:")
            for mo in sorted({d[:7] for d in days[s]}):
                sel = top & (np.array([d[:7] for d in days[s]]) == mo)
                x = y[s][sel] - COST
                print(f"     {mo}: n={sel.sum():4d} {x.mean() if len(x) else 0:+.3f}±{x.std() / np.sqrt(max(1, len(x))):.3f}")
            imp = sorted(zip(NAMES, np.round(np.abs(np.corrcoef(np.nan_to_num(X[split['train'] & ok]).T,
                                                               y[split['train'] & ok])[-1, :-1]), 3)),
                         key=lambda t: -t[1])[:8]
            print("   |corr| with fwd60 on train:", imp)


if __name__ == "__main__":
    main()
