"""Cheap text baseline for the liquid_fade filter: can the news text pick which big news-day moves fade over 5 days?

    .venv/bin/python scripts/fade_text_baseline.py

Events: the first news of each (ticker, day) in data/laya_fade_labels.jsonl (scripts/build_fade_labels.py). Trade: short at
the news day's close, cover 5 trading days later, P&L = -(5-day return minus SPY's) - 0.2% cost. Model: TF-IDF of headline
and summary plus the day's move (sign and size), ridge regression on the clipped P&L. Split by time: train to 2024,
valid 2025-01..2025-10 (picks the cut), test 2025-11 onward (same test period as the Laya d5 runs). The live book's
threshold (|day move| >= 10%) is applied to valid and test; training also uses the 8-10% band.
"""
import json
import math
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from scipy.sparse import csr_matrix, hstack
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import Ridge

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
from jev_compare import day_t  # noqa: E402

COST = 0.2


def events():
    seen, out = set(), []
    rows = [json.loads(line) for line in open(ROOT / "data" / "laya_fade_labels.jsonl")]
    for r in sorted(rows, key=lambda r: r["ts"]):
        k = (r["ticker"], r["ts"][:10])
        if k in seen:
            continue
        seen.add(k)
        r["pnl"] = -r["d5_excess"] * 100 - COST
        out.append(r)
    return out


def num(r):
    d = r["day_excess"]
    return [d > 0, abs(d), abs(d) >= 0.2, min(r["n_tickers"], 5) / 5]


def show(name, rs):
    if not rs:
        print(f"  {name:38s} n=0")
        return
    mu, t = day_t([r["pnl"] for r in rs], [r["ts"][:10] for r in rs])
    print(f"  {name:38s} n={len(rs):5d}  {mu:+6.2f}%/trade  t {t:+5.1f}")


def main():
    ev = events()
    train = [r for r in ev if r["ts"] < "2025-01-01"]
    valid = [r for r in ev if "2025-01-01" <= r["ts"] < "2025-11-01" and abs(r["day_excess"]) >= 0.10]
    test = [r for r in ev if r["ts"] >= "2025-11-01" and abs(r["day_excess"]) >= 0.10]
    text = lambda r: f"{'UP' if r['day_excess'] > 0 else 'DOWN'} {r['headline']} {r.get('summary') or ''}"
    vec = TfidfVectorizer(ngram_range=(1, 2), min_df=5, max_features=50000, sublinear_tf=True, stop_words="english")
    X = lambda rs, fit=False: hstack([(vec.fit_transform if fit else vec.transform)([text(r) for r in rs]),
                                      csr_matrix(np.array([num(r) for r in rs], dtype=float))]).tocsr()
    Xtr = X(train, True)
    ytr = np.clip([r["pnl"] for r in train], -30, 30)
    print(f"train {len(train)} events (to 2024), valid {len(valid)}, test {len(test)} (|day move| >= 10%)")
    for alpha in (3.0, 10.0, 30.0):
        m = Ridge(alpha=alpha).fit(Xtr, ytr)
        pv, pt = m.predict(X(valid)), m.predict(X(test))
        cut = float(np.quantile(pv, 1 / 3))  # skip the third of fades the model likes least (cut set on valid)
        print(f"\n== ridge alpha {alpha}: corr(pred, pnl) valid {np.corrcoef(pv, [r['pnl'] for r in valid])[0, 1]:+.3f} "
              f"test {np.corrcoef(pt, [r['pnl'] for r in test])[0, 1]:+.3f}; skip cut {cut:+.2f}")
        for name, rs, p in (("valid", valid, pv), ("test", test, pt)):
            show(f"{name} all fades", rs)
            show(f"{name} kept (pred >= cut)", [r for r, x in zip(rs, p) if x >= cut])
            show(f"{name} skipped", [r for r, x in zip(rs, p) if x < cut])
            for side in ("up", "down"):
                show(f"{name} {side} moves", [r for r in rs if (r["day_excess"] > 0) == (side == "up")])
        if alpha == 10.0:
            names = vec.get_feature_names_out().tolist() + ["is_up", "abs_move", "move>=20%", "n_tickers"]
            order = np.argsort(m.coef_)
            print("  words -> fade loses: " + ", ".join(names[i] for i in order[:25]))
            print("  words -> fade wins:  " + ", ".join(names[i] for i in order[-25:][::-1]))


if __name__ == "__main__":
    main()
