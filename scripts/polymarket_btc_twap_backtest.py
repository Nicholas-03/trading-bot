"""Latency rule for the Bitcoin up/down markets under the current (TWAP) settlement, on data recorded live by
scripts/polymarket_btc_record.py.

    .venv/bin/python scripts/polymarket_btc_twap_backtest.py data/btc_live/*.jsonl [--fee 0.0175]

Settlement since August 2026: Up if X(end) >= X(start), X(t) = Chainlink BTC/USD averaged over the 60 seconds up to t
(gamma's eventMetadata priceToBeat / finalPrice; checked here against the recorded Chainlink feed).
Each second, using only what had arrived by then (local receive times):
  P   = last Chainlink price moved forward by Binance's change since that Chainlink print (Binance leads by ~1 s)
  sd  = per-second volatility of Binance over the previous 15 minutes
  fair P(up): more than 60 s left -> Phi(ln(P / K) / (sd * sqrt(left - 40)))   (the end average of a random walk)
              less than 60 s left -> known part of the final average + P for the rest, spread (left/60) * sd * sqrt(left/3)
We then take the book as it stands `latency` seconds later and buy 5+ shares of Up (Down) at its ask when fair (1 - fair)
beats the ask by the threshold; one trade per side per market per 30 s; held to settlement; taker fee included.
"""
import argparse
import bisect
import json
import math
import urllib.request
from collections import defaultdict
from pathlib import Path

import numpy as np

UA = {"User-Agent": "Mozilla/5.0"}


def phi(x: float) -> float:
    return 0.5 * (1 + math.erf(x / math.sqrt(2)))


def load(paths: list[str]):
    markets, cl, bn, books = {}, [], [], defaultdict(list)
    for p in paths:
        for line in open(p):
            try:
                r = json.loads(line)
            except ValueError:  # a line cut short when the recorder stopped
                continue
            k = r["k"]
            if k == "bk":
                books[r["a"]].append((r["t"], r["b"], r["bs"], r["x"], r["xs"]))
            elif k == "bn":
                bn.append((r["t"], r["p"]))
            elif k == "cl":
                cl.append((r["t"], r["ts"], r["v"]))
            elif k == "m":
                markets[r["slug"]] = r
    for k, v in books.items():
        v.sort(key=lambda r: r[0])
        books[k] = ([r[0] for r in v], v)
    return markets, sorted(cl), sorted(bn), books


def settle(slug: str) -> dict | None:
    with urllib.request.urlopen(urllib.request.Request(f"https://gamma-api.polymarket.com/events?slug={slug}", headers=UA), timeout=20) as r:
        d = json.load(r)
    if not d or not d[0]["markets"][0].get("closed"):
        return None
    meta = d[0].get("eventMetadata") or {}
    return {"up": json.loads(d[0]["markets"][0]["outcomePrices"])[0] == "1", "k": meta.get("priceToBeat"), "final": meta.get("finalPrice")}


class Feeds:
    def __init__(self, cl, bn):
        self.cl_t = [x[0] for x in cl]  # receive time
        self.cl = cl
        by_ts = {}
        for t, ts, v in cl:
            by_ts.setdefault(ts, v)
        self.cl_ts = sorted(by_ts)
        self.cl_v = [by_ts[t] for t in self.cl_ts]
        self.bn_t = np.array([x[0] for x in bn])
        self.bn_p = np.array([x[1] for x in bn])

    def twap(self, t_ms: int) -> float | None:
        """Mean Chainlink price over (t - 60 s, t], by Chainlink's own timestamps."""
        i, j = bisect.bisect_right(self.cl_ts, t_ms - 60000), bisect.bisect_right(self.cl_ts, t_ms)
        return float(np.mean(self.cl_v[i:j])) if j - i >= 50 else None

    def bn_at(self, t_ms: int) -> float:
        return self.bn_p[max(0, np.searchsorted(self.bn_t, t_ms, side="right") - 1)]

    def known(self, now: int):
        """Last Chainlink print received by `now`: (its timestamp, value), and the Chainlink prints known by then."""
        i = bisect.bisect_right(self.cl_t, now) - 1
        return (self.cl[i][1], self.cl[i][2]) if i >= 0 else None

    def sd(self, now: int) -> float:
        px = self.bn_p[np.maximum(0, np.searchsorted(self.bn_t, np.arange(now - 900000, now + 1, 1000), side="right") - 1)]
        return float(np.diff(np.log(px)).std())


def fair_up(f: Feeds, now: int, start: int, end: int, k: float) -> float | None:
    last = f.known(now)
    if last is None or now - last[0] > 5000:
        return None
    p = last[1] * f.bn_at(now) / f.bn_at(last[0])  # Chainlink moved on by Binance's change since that print
    sd = f.sd(now)
    if sd <= 0:
        return None
    left = (end - now) / 1000
    if left >= 60:
        return phi(math.log(p / k) / (sd * math.sqrt(left - 40)))
    i = bisect.bisect_right(f.cl_ts, end - 60000)
    j = bisect.bisect_right(f.cl_ts, last[0])
    done = f.cl_v[i:j]  # prints inside the final window already seen
    gone = 60 - left
    a = float(np.mean(done)) if done else p
    mean_final = (a * gone + p * left) / 60
    spread = (left / 60) * sd * p * math.sqrt(left / 3)
    return phi((mean_final - k) / spread) if spread > 0 else float(mean_final >= k)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("files", nargs="+")
    ap.add_argument("--fee", type=float, default=0.0175)
    args = ap.parse_args()
    markets, cl, bn, books = load(args.files)
    f = Feeds(cl, bn)
    t_lo, t_hi = max(cl[0][0], bn[0][0]) + 900000, min(cl[-1][0], bn[-1][0])
    res, err = {}, []
    for slug, m in markets.items():
        if m["start"] * 1000 >= t_lo and m["end"] * 1000 <= t_hi:
            s = settle(slug)
            if s and s["k"]:
                res[slug] = s
                for t, x in ((m["start"], s["k"]), (m["end"], s["final"])):
                    tw = f.twap(t * 1000)
                    if tw and x:
                        err.append(tw - x)
    err = np.array(err)
    print(f"{len(res)} settled markets; recorded 60 s Chainlink average minus gamma's price: "
          f"mean {err.mean():+.3f} $, abs max {np.abs(err).max():.3f} $  (n={len(err)})")
    trades = {lat: defaultdict(list) for lat in (0.2, 0.5, 1.0)}
    for slug, s in res.items():
        m = markets[slug]
        start, end = m["start"] * 1000, m["end"] * 1000
        ser = "5m" if end - start == 300000 else "15m"
        for lat, by in trades.items():
            last = {}
            for now in range(start + 5000, end - 2000, 1000):
                fu = fair_up(f, now, start, end, s["k"])
                if fu is None:
                    continue
                for side, tok, pw, win in (("up", m["up"][-8:], fu, s["up"]), ("down", m["down"][-8:], 1 - fu, not s["up"])):
                    bk = books.get(tok)
                    if not bk:
                        continue
                    i = bisect.bisect_right(bk[0], now + lat * 1000) - 1
                    if i < 0 or bk[1][i][3] is None:
                        continue
                    ask, size = bk[1][i][3], bk[1][i][4]
                    edge = pw - ask
                    for thr in (0.03, 0.05, 0.10, 0.20):
                        if edge >= thr and size >= 5 and 0.02 <= ask <= 0.98 and now - last.get((thr, side), -1e12) >= 30000:
                            last[(thr, side)] = now
                            fee = args.fee * ask * (1 - ask) / 0.25
                            by[thr].append({"series": ser, "market": slug, "pnl": (1.0 if win else 0.0) - ask - fee,
                                            "edge": edge, "price": ask, "left": (end - now) / 1000})
    for lat, by in trades.items():
        print(f"\n== latency {lat}s")
        for thr, xs in sorted(by.items()):
            per = defaultdict(list)
            for x in xs:
                per[x["market"]].append(x["pnl"] * 100)
            mm = np.array([np.mean(v) for v in per.values()])
            v = np.array([x["pnl"] for x in xs]) * 100
            late = np.array([x["pnl"] for x in xs if x["left"] < 60]) * 100
            print(f"threshold {thr:.2f}: n={len(v)} in {len(mm)} markets, model edge {np.mean([x['edge'] for x in xs]) * 100:.1f}¢, "
                  f"realised {v.mean():+.2f}¢/share (market-averaged {mm.mean():+.2f}¢ ± {mm.std() / math.sqrt(len(mm)):.2f}, "
                  f"won {np.mean(mm > 0):.0%}); last minute {late.mean() if len(late) else float('nan'):+.2f}¢ (n={len(late)})")
            for ser in ("5m", "15m"):
                wm = [np.mean(per[k]) for k in {x["market"] for x in xs if x["series"] == ser}]
                if wm:
                    print(f"    {ser:4s} {len(wm):3d} markets  {np.mean(wm):+.2f}¢ ± {np.std(wm) / math.sqrt(len(wm)):.2f}")


if __name__ == "__main__":
    main()
